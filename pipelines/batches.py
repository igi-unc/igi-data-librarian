"""Automatic RAFT runs: one project per patient, re-run as new samples arrive.

For each workflow with ``auto_run = true``:

* **enqueue** - when a compatible dataset is accessioned, it joins the RAFT project for its
  patient (``<id_prefix>-<workflow>-<patient>``). If no timer is running for that project, this
  sample starts one (``auto_delay_minutes``). Later samples join the same timer; they do not
  extend it.
* **dispatch** - when a timer ends and the project is not already running, the bot symlinks
  every sample's inputs, rewrites the project's manifest with all of the patient's samples,
  checks it, and submits one job: ``raft run --setup-only`` the first time, then
  ``raft update-mounts`` and ``raft run``. Nextflow's resume (RAFT's default) skips work that
  already finished, so only the new samples are processed.
* **poll** - when the job ends, every sample in that job is marked completed or failed.
  Samples that arrived while it ran already have their own timer and go in the next run.

Project IDs carry only the instance prefix, workflow and participant ID (safe to post).
"""

from __future__ import annotations

import csv
import shlex
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from instance import RaftWorkflow

from .raft import (
    MANIFEST_COLUMNS,
    RaftError,
    RaftRunner,
    _token,
    input_files,
    manifest_row,
    patient_of,
    run_name_for,
)

# Accessions (Slack threads) and the monitor thread both change project state.
_LOCK = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Queued:
    """What happened to one dataset for one automatic workflow."""

    workflow_name: str
    project_id: str
    patient: str
    due_at: datetime
    started_timer: bool  # this sample started the project's timer
    project_running: bool  # an earlier run of this project is still going


@dataclass(frozen=True)
class BatchUpdate:
    """A per-patient project event worth telling people about."""

    project_id: str
    patient: str
    workflow_name: str
    status: str  # started | completed | failed
    detail: str
    channel_id: str
    requested_by: tuple[str, ...] = field(default_factory=tuple)
    samples: int = 0
    new_samples: int = 0


def project_id_for_patient(prefix: str, workflow: RaftWorkflow, patient: str) -> str:
    return f"{prefix}-{_token(workflow.id)}-{patient}".lower()


class PatientBatcher:
    def __init__(self, runner: RaftRunner):
        self.runner = runner
        self.registry = runner.registry
        self.config = runner.config
        self.instance = runner.instance

    # -- enqueue ----------------------------------------------------------------------
    def enqueue(self, dataset_internal_id: str, requested_by: str, channel_id: str,
                now: datetime | None = None) -> list[Queued]:
        """Add a newly accessioned dataset to its patient's project for each automatic workflow."""
        now = now or _utcnow()
        dataset = self.registry.dataset(dataset_internal_id)
        queued = []
        with _LOCK:
            for workflow in self.config.auto_workflows:
                if not self.runner.compatible(workflow, dataset):
                    continue
                patient = patient_of(dataset)
                project_id = project_id_for_patient(self.instance.id_prefix, workflow, patient)
                runs = [run for run in self.registry.project_runs(project_id) if run["status"] != "cancelled"]
                if any(run["dataset_internal_id"] == dataset_internal_id for run in runs):
                    continue
                run_name = run_name_for(dataset)
                if any(run["parameters"].get("run_name") == run_name for run in runs):
                    run_name = f"{run_name}-{dataset_internal_id.replace('-', '')[:6]}"
                self.registry.initialize()
                self.registry.create_workflow_run(
                    workflow.workflow_id, dataset_internal_id, requested_by,
                    {"auto": True, "project_id": project_id, "patient": patient, "run_name": run_name,
                     "channel_id": channel_id, "raft_workflow": workflow.workflow,
                     "raft_version": workflow.version, "input_data": workflow.input_data},
                )
                project = self.registry.raft_project(project_id)
                started_timer = not (project and project["due_at"])
                due_at = now + timedelta(minutes=workflow.auto_delay_minutes) if started_timer else _parse(project["due_at"])
                fields = {"workflow_id": workflow.workflow_id, "patient": patient}
                if started_timer:
                    fields["due_at"] = _iso(due_at)
                if channel_id:
                    fields["channel_id"] = channel_id
                project = self.registry.save_raft_project(project_id, **fields)
                queued.append(Queued(workflow.name, project_id, patient, due_at, started_timer,
                                     project["status"] == "running"))
        return queued

    # -- dispatch ---------------------------------------------------------------------
    def dispatch_due(self, now: datetime | None = None) -> list[BatchUpdate]:
        """Launch every project whose timer has ended and that is not already running."""
        now = now or _utcnow()
        updates = []
        with _LOCK:
            for project in self.registry.raft_projects():
                due_at = _parse(project["due_at"])
                if project["status"] == "running" or not due_at or due_at > now:
                    continue
                try:
                    update = self._launch(project)
                except Exception as exc:  # report and move on to the next project
                    update = self._fail_pending(project, str(exc))
                if update:
                    updates.append(update)
        return updates

    def _launch(self, project: dict) -> BatchUpdate | None:
        project_id = project["project_id"]
        workflow = self.config.workflow(project["workflow_id"])
        runs = [run for run in self.registry.project_runs(project_id) if run["status"] != "cancelled"]
        new_runs = [run for run in runs if run["status"] == "requested"]
        if not new_runs:
            self.registry.save_raft_project(project_id, due_at=None)
            return None
        if not workflow.enabled:
            raise RaftError(f"{workflow.name} is no longer enabled.")
        self.runner._check_workspace()

        rows, staged_dirs = [], set()
        for run in runs:
            dataset = self.registry.dataset(run["dataset_internal_id"])
            row = manifest_row(dataset, project_id, run["parameters"].get("run_name"))
            files = input_files(dataset, workflow.input_data)
            links = self.runner._stage(project_id, workflow.input_data, row["File_Prefix"], files)
            staged_dirs.update(link.parent for link in links)
            rows.append(row)
        manifest = self.write_manifest(project_id, sorted(rows, key=lambda row: row["Run_Name"]))
        log = self.runner._log_path(project_id, "run")
        self.runner._raft(["check-manifest", "--manifest", str(manifest)], log, timeout=300)

        script = self.job_script(workflow, project_id, manifest, staged_dirs)
        job_id = self.runner.submit(project_id, script, log)
        for run in new_runs:
            self.registry.update_workflow_run(run["run_id"], "running", scheduler_job_id=job_id)
        self.registry.save_raft_project(project_id, status="running", due_at=None, scheduler_job_id=job_id)
        self.registry.record_event("raft_project_launched", None, {
            "project_id": project_id, "job": job_id, "samples": len(rows), "new_samples": len(new_runs)})
        return BatchUpdate(
            project_id=project_id, patient=project["patient"], workflow_name=workflow.name, status="started",
            detail=f"job {job_id}", channel_id=project["channel_id"],
            requested_by=tuple(sorted({run["requested_by"] for run in new_runs})),
            samples=len(rows), new_samples=len(new_runs),
        )

    def write_manifest(self, project_id: str, rows: list[dict]) -> Path:
        """Rewrite the project's manifest in place (RAFT links the project to this file)."""
        manifest = self.runner.workspace / "inputs" / "metadata" / f"{project_id}.manifest.tsv"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        with manifest.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        return manifest

    def job_script(self, workflow: RaftWorkflow, project_id: str, manifest: Path, staged_dirs: set[Path]) -> str:
        raft = self.config.executable
        steps = []
        if not (self.runner.workspace / "projects" / project_id).is_dir():
            setup = [raft, "run", "--project-id", project_id, "--workflow", workflow.workflow,
                     "--version", workflow.version, "--species", workflow.species,
                     "--input-data", workflow.input_data, "--manifest", str(manifest), "--setup-only"]
            if workflow.workflow_profile:
                setup += ["--workflow-profile", workflow.workflow_profile]
            steps.append(setup)
        for directory in sorted(staged_dirs):
            # Bind the real locations of newly symlinked inputs into the containers.
            steps.append([raft, "update-mounts", "--project-id", project_id, "--dir", str(directory)])
        steps.append(self.runner.run_command(project_id))
        return " && ".join(shlex.join(step) for step in steps)

    def _fail_pending(self, project: dict, error: str) -> BatchUpdate:
        project_id = project["project_id"]
        pending = [run for run in self.registry.project_runs(project_id) if run["status"] == "requested"]
        for run in pending:
            self.registry.update_workflow_run(run["run_id"], "failed", error_message=error)
        self.registry.save_raft_project(project_id, due_at=None)
        try:
            workflow_name = self.config.workflow(project["workflow_id"]).name
        except KeyError:
            workflow_name = project["workflow_id"]
        return BatchUpdate(
            project_id=project_id, patient=project["patient"], workflow_name=workflow_name, status="failed",
            detail=f"could not start: {error}", channel_id=project["channel_id"],
            requested_by=tuple(sorted({run["requested_by"] for run in pending})), new_samples=len(pending),
        )

    # -- poll -------------------------------------------------------------------------
    def poll(self, now: datetime | None = None) -> list[BatchUpdate]:
        """Record finished per-patient runs."""
        now = now or _utcnow()
        updates = []
        for project in self.registry.raft_projects(status="running"):
            project_id, job_id = project["project_id"], project["scheduler_job_id"] or ""
            result = self.runner.outcome(job_id, project_id)
            if result is None:
                continue
            status, detail = result
            with _LOCK:
                runs = [run for run in self.registry.project_runs(project_id)
                        if run["status"] == "running" and run["scheduler_job_id"] == job_id]
                for run in runs:
                    self.registry.update_workflow_run(run["run_id"], status,
                                                      error_message=None if status == "completed" else detail)
                    if status == "completed":
                        self.registry.register_output_artifact(
                            run["run_id"], "raft_project", str(self.runner.workspace / "projects" / project_id),
                            metadata={"detail": detail},
                        )
                fields = {"status": "idle", "scheduler_job_id": None}
                waiting = [run for run in self.registry.project_runs(project_id) if run["status"] == "requested"]
                if waiting and not project["due_at"]:
                    fields["due_at"] = _iso(now)  # safety net: never strand a waiting sample
                self.registry.save_raft_project(project_id, **fields)
            try:
                workflow_name = self.config.workflow(project["workflow_id"]).name
            except KeyError:
                workflow_name = project["workflow_id"]
            updates.append(BatchUpdate(
                project_id=project_id, patient=project["patient"], workflow_name=workflow_name, status=status,
                detail=detail, channel_id=project["channel_id"],
                requested_by=tuple(sorted({run["requested_by"] for run in runs})),
                samples=sum(run["status"] != "cancelled" for run in self.registry.project_runs(project_id)),
                new_samples=len(runs),
            ))
        return updates
