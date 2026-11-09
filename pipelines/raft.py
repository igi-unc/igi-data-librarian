"""Set up, launch and monitor RAFT workflows on registered datasets.

The bot uses its own RAFT workspace (a directory with its own ``.raft.cfg``, created once
with ``raft setup -d`` inside it). For each request it:

1. **prepare** - symlinks the dataset's registered FASTQs/BAMs into the workspace's
   ``inputs/<fastqs|bams>/<project>/`` directory (files are never copied or moved), writes
   a manifest, runs ``raft check-manifest`` and ``raft run ... --setup-only``;
2. **launch** - after a person approves, runs ``raft run -p <project>`` through ``sbatch``
   (or as a detached local process);
3. **poll** - checks the scheduler and ``raft project-status --json`` until the run ends.

RAFT project IDs are ``<id_prefix>-<workflow>-<8 hex>``; they carry no sample identifiers,
so they are safe to post in channels.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from instance import InstanceConfig, RaftWorkflow
from registry import RegistryService

FASTQ_SUFFIXES = (".fastq.gz", ".fq.gz", ".fastq", ".fq")
BAM_SUFFIXES = (".bam", ".bam.bai", ".bai")
INPUT_SUFFIXES = {
    "fastqs": FASTQ_SUFFIXES,
    "bams": BAM_SUFFIXES,
    "multi": FASTQ_SUFFIXES + BAM_SUFFIXES,  # RAFT finds each by File_Prefix under fastqs/ and bams/
}
# The LENS v2 manifest; RAFT qc checks Platform/Read_Type against the data.
MANIFEST_COLUMNS = ["Patient_Name", "Run_Name", "Dataset", "File_Prefix", "Sequencing_Method", "Normal",
                    "Group", "Platform", "Read_Type", "Sample_Type"]
SEQUENCING_METHODS = {"wgs": "WGS", "wes": "WES", "poly-a": "RNA-Seq"}
PLATFORMS = {"illumina": ("Illumina", "short-read"), "ont": ("ONT", "long-read"),
             "pacbio": ("PacBio", "long-read"), "10x-ont": ("ONT", "long-read")}
SAMPLE_TYPES = {"tumor": "tumor", "normal-blood": "blood-normal", "normal-tissue": "tissue-normal",
                "cell-line": "cell-line"}
ACTIVE_SLURM_STATES = {"PENDING", "CONFIGURING", "RUNNING", "COMPLETING", "SUSPENDED", "REQUEUED", "RESIZING"}

Runner = Callable[..., subprocess.CompletedProcess]


class RaftError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunUpdate:
    """A status change worth telling people about."""

    run_id: str
    project_id: str
    workflow_name: str
    status: str  # running | completed | failed
    detail: str
    requested_by: str
    channel_id: str


def _token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value or "").strip("-")


def project_id_for(instance: InstanceConfig, workflow: RaftWorkflow, run_id: str) -> str:
    return f"{instance.id_prefix}-{_token(workflow.id)}-{run_id.replace('-', '')[:8]}".lower()


def run_name_for(dataset: dict) -> str:
    """RAFT Run_Name (also the FASTQ File_Prefix): a|n (tumor/normal), d|r (DNA/RNA), LIMS number."""
    row = dataset["row"]
    normal = (row.get("sample_type") or "").startswith("normal")
    short = row.get("lims_number") or dataset["internal_id"].replace("-", "")[:8]
    return f"{'n' if normal else 'a'}{'r' if row.get('analyte') == 'rna' else 'd'}-{_token(short)}"


def patient_of(dataset: dict) -> str:
    row = dataset["row"]
    return _token(row.get("source") or row.get("lims_number") or dataset["internal_id"][:8])


def manifest_row(dataset: dict, project_id: str, run_name: str | None = None) -> dict[str, str]:
    """Build the RAFT manifest row for one registered dataset."""
    row = dataset["row"]
    normal = (row.get("sample_type") or "").startswith("normal")
    analyte = row.get("analyte") or ""
    library = row.get("library_type") or ""
    method = "RNA-Seq" if analyte == "rna" else SEQUENCING_METHODS.get(library, "")
    if not method:
        raise RaftError("Set the dataset's library type (wgs, wes or poly-a) or analyte before running RAFT.")
    run_name = run_name or run_name_for(dataset)
    return {
        "Patient_Name": patient_of(dataset),
        "Run_Name": run_name,
        "Dataset": project_id,
        "File_Prefix": run_name,
        "Sequencing_Method": method,
        "Normal": "TRUE" if normal else "FALSE",
        "Group": "",
        "Platform": PLATFORMS.get(row.get("platform") or "", ("", ""))[0],
        "Read_Type": PLATFORMS.get(row.get("platform") or "", ("", ""))[1],
        "Sample_Type": SAMPLE_TYPES.get(row.get("sample_type") or "", row.get("sample_type") or ""),
    }


def input_files(dataset: dict, input_data: str) -> list[Path]:
    suffixes = INPUT_SUFFIXES[input_data]
    files = [Path(path) for path in dataset["files"] if path.lower().endswith(suffixes)]
    primary = [path for path in files if not path.name.lower().endswith((".bai",))]
    if not primary:
        kind = {"fastqs": "FASTQ", "bams": "BAM"}.get(input_data, "FASTQ or BAM")
        raise RaftError(f"This dataset has no registered {kind} files.")
    missing = [path for path in files if not path.exists()]
    if missing:
        raise RaftError(f"{len(missing)} registered input file(s) are missing on disk.")
    return files


class RaftRunner:
    def __init__(self, instance: InstanceConfig, registry: RegistryService, run: Runner = subprocess.run):
        if instance.raft is None:
            raise RaftError(f"{instance.name} has no [raft] configuration.")
        self.instance = instance
        self.config = instance.raft
        self.registry = registry
        self._run = run

    # -- paths -------------------------------------------------------------------------
    @property
    def workspace(self) -> Path:
        return self.config.workspace

    def _log_path(self, project_id: str, stage: str) -> Path:
        path = self.workspace / "bot-logs" / f"{project_id}.{stage}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _check_workspace(self) -> None:
        if not (self.workspace / ".raft.cfg").is_file():
            raise RaftError(f"The RAFT workspace has no .raft.cfg; run `raft setup -d` in {self.workspace}.")

    def _raft(self, args: list[str], log: Path, timeout: int) -> subprocess.CompletedProcess:
        command = [self.config.executable, *args]
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"$ {shlex.join(command)}\n")
            handle.flush()
            try:
                result = self._run(
                    command, cwd=self.workspace, stdin=subprocess.DEVNULL, stdout=handle,
                    stderr=subprocess.STDOUT, timeout=timeout, check=False,
                )
            except subprocess.TimeoutExpired:
                raise RaftError(f"`raft {args[0]}` timed out after {timeout} s; see {log}.") from None
        if result.returncode != 0:
            raise RaftError(f"`raft {args[0]}` failed (exit {result.returncode}); see {log}.")
        return result

    # -- requests ----------------------------------------------------------------------
    @staticmethod
    def compatible(workflow: RaftWorkflow, dataset: dict) -> bool:
        """Whether a workflow can run on a dataset (analyte, library type and input files)."""
        row = dataset["row"]
        if workflow.compatible_analytes and row.get("analyte") not in workflow.compatible_analytes:
            return False
        if workflow.compatible_library_types and row.get("library_type") not in workflow.compatible_library_types:
            return False
        suffixes = INPUT_SUFFIXES[workflow.input_data]
        return any(path.lower().endswith(suffixes) for path in dataset["files"])

    def eligible_datasets(self, workflow: RaftWorkflow) -> list[dict]:
        """Registered datasets this workflow can run on (input type and compatibility)."""
        return [dataset for dataset in self.registry.datasets() if self.compatible(workflow, dataset)]

    def request(self, workflow_key: str, dataset_internal_id: str, user_id: str, channel_id: str) -> tuple[str, str]:
        """Record a run request. Returns (run_id, project_id)."""
        workflow = self.config.workflow(workflow_key)
        if not workflow.enabled:
            raise RaftError(f"{workflow.name} is not enabled yet.")
        self.registry.initialize()  # makes sure the workflow definition row exists
        dataset = self.registry.dataset(dataset_internal_id)
        input_files(dataset, workflow.input_data)
        run_id = self.registry.create_workflow_run(
            workflow.workflow_id, dataset_internal_id, user_id,
            {"channel_id": channel_id, "raft_workflow": workflow.workflow, "raft_version": workflow.version,
             "species": workflow.species, "input_data": workflow.input_data},
        )
        project_id = project_id_for(self.instance, workflow, run_id)
        run = self.registry.workflow_run(run_id)
        parameters = {**run["parameters"], "project_id": project_id}
        self.registry.update_workflow_run(run_id, "requested", parameters_json=parameters)
        return run_id, project_id

    def prepare(self, run_id: str) -> dict:
        """Stage inputs, write and check the manifest, and run RAFT setup (no launch)."""
        run = self.registry.workflow_run(run_id)
        parameters = run["parameters"]
        project_id = parameters["project_id"]
        workflow = self.config.workflow(run["workflow_id"])
        self.registry.update_workflow_run(run_id, "preparing")
        try:
            self._check_workspace()
            dataset = self.registry.dataset(run["dataset_internal_id"])
            row = manifest_row(dataset, project_id)
            staged = self._stage(project_id, workflow.input_data, row["File_Prefix"],
                                 input_files(dataset, workflow.input_data))
            manifest = self.workspace / "inputs" / "metadata" / f"{project_id}.manifest.tsv"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            with manifest.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS, delimiter="\t", lineterminator="\n")
                writer.writeheader()
                writer.writerow(row)
            log = self._log_path(project_id, "setup")
            self._raft(["check-manifest", "--manifest", str(manifest)], log, timeout=300)
            setup = ["run", "--project-id", project_id, "--workflow", workflow.workflow,
                     "--version", workflow.version, "--species", workflow.species,
                     "--input-data", workflow.input_data, "--manifest", str(manifest), "--setup-only"]
            if workflow.workflow_profile:
                setup += ["--workflow-profile", workflow.workflow_profile]
            self._raft(setup, log, timeout=self.config.setup_timeout_seconds)
        except Exception as exc:
            self.registry.update_workflow_run(run_id, "failed", error_message=str(exc))
            raise
        parameters = {**parameters, "manifest": str(manifest), "staged_files": len(staged)}
        self.registry.update_workflow_run(run_id, "ready", parameters_json=parameters)
        return {"project_id": project_id, "workflow": workflow, "staged_files": len(staged),
                "manifest": str(manifest), "dataset": dataset}

    def staging_dir(self, project_id: str, input_data: str, source: Path) -> Path:
        """inputs/fastqs/<project> or inputs/bams/<project> (multi puts each file by its type)."""
        if input_data == "multi":
            input_data = "bams" if source.name.lower().endswith(BAM_SUFFIXES) else "fastqs"
        return self.workspace / "inputs" / input_data / project_id

    def _stage(self, project_id: str, input_data: str, prefix: str, files: list[Path]) -> list[Path]:
        """Symlink registered inputs as <inputs>/<fastqs|bams>/<project>/<File_Prefix>_<original name>."""
        links = []
        for source in files:
            target_dir = self.staging_dir(project_id, input_data, source)
            target_dir.mkdir(parents=True, exist_ok=True)
            link = target_dir / f"{prefix}_{source.name}"
            if link.is_symlink() or link.exists():
                if link.is_symlink() and Path(os.readlink(link)) == source.resolve():
                    links.append(link)
                    continue
                raise RaftError(f"{link} already exists and points elsewhere.")
            link.symlink_to(source.resolve())
            links.append(link)
        return links

    def launch(self, run_id: str, approved_by: str) -> str:
        """Start a prepared run. Returns the scheduler job ID."""
        if not self.instance.is_approver(approved_by):
            raise PermissionError("Only approvers can launch RAFT runs.")
        run = self.registry.workflow_run(run_id)
        if run["status"] != "ready":
            raise RaftError(f"This run is {run['status']}, not ready to launch.")
        project_id = run["parameters"]["project_id"]
        log = self._log_path(project_id, "run")
        job_id = self.submit(project_id, shlex.join(self.run_command(project_id)), log)
        parameters = {**run["parameters"], "approved_by": approved_by, "run_log": str(log)}
        self.registry.update_workflow_run(run_id, "running", scheduler_job_id=job_id, parameters_json=parameters)
        self.registry.record_event("workflow_launched", run["dataset_internal_id"],
                                   {"run_id": run_id, "project_id": project_id, "job": job_id,
                                    "approved_by": approved_by})
        return job_id

    def run_command(self, project_id: str) -> list[str]:
        """`raft run` for an existing project; Nextflow resumes from its cache by default."""
        command = [self.config.executable, "run", "--project-id", project_id]
        if self.config.nextflow_profile:
            command += ["--profile", self.config.nextflow_profile]
        return command

    def submit(self, project_id: str, script: str, log: Path) -> str:
        """Start a shell script as a Slurm job (or detached process). Returns the job ID."""
        if self.config.launcher == "sbatch":
            sbatch = ["sbatch", "--parsable", "--job-name", project_id, "--output", str(log),
                      "--chdir", str(self.workspace), *self.config.sbatch_args, "--wrap", script]
            result = self._run(sbatch, cwd=self.workspace, capture_output=True, text=True, timeout=60, check=False)
            if result.returncode != 0:
                raise RaftError(f"sbatch failed (exit {result.returncode}): {(result.stderr or '').strip()[:300]}")
            return f"slurm:{result.stdout.strip().split(';')[0]}"
        with log.open("a", encoding="utf-8") as handle:
            process = subprocess.Popen(
                ["/bin/sh", "-c", script], cwd=self.workspace, stdin=subprocess.DEVNULL, stdout=handle,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
        return f"pid:{process.pid}"

    def outcome(self, job_id: str, project_id: str) -> tuple[str, str] | None:
        """None while the job is active; otherwise (completed|failed, detail)."""
        active, scheduler_state = self._job_state(job_id or "")
        if active:
            return None
        state = self.project_state(project_id)
        label = state.get("label") or "no RAFT status"
        if state.get("id") == "run_failed" or scheduler_state in {"FAILED", "CANCELLED", "TIMEOUT",
                                                                  "OUT_OF_MEMORY", "NODE_FAIL"}:
            status = "failed"
        elif state.get("id") in {"reports_available", "run_started_no_reports"} or scheduler_state == "COMPLETED":
            status = "completed"
        else:
            status = "failed"
        return status, f"{label}; scheduler: {scheduler_state}"

    def cancel(self, run_id: str, user_id: str) -> None:
        run = self.registry.workflow_run(run_id)
        if run["status"] not in {"requested", "preparing", "ready"}:
            raise RaftError(f"This run is {run['status']} and cannot be cancelled here.")
        self.registry.update_workflow_run(run_id, "cancelled", error_message=f"Cancelled by {user_id} before launch")

    # -- monitoring --------------------------------------------------------------------
    def _job_state(self, job_id: str) -> tuple[bool, str]:
        """(still active, scheduler state)."""
        kind, _, value = (job_id or "").partition(":")
        if kind == "pid" and value.isdigit():
            try:
                os.kill(int(value), 0)
                return True, "RUNNING"
            except ProcessLookupError:
                return False, "EXITED"
            except PermissionError:
                return True, "RUNNING"
        if kind == "slurm" and value:
            queue = self._run(["squeue", "-h", "-j", value, "-o", "%T"], capture_output=True, text=True,
                              timeout=60, check=False)
            state = (queue.stdout or "").strip().splitlines()
            if queue.returncode == 0 and state and state[0] in ACTIVE_SLURM_STATES:
                return True, state[0]
            account = self._run(["sacct", "-n", "-X", "-j", value, "-o", "State"], capture_output=True,
                                text=True, timeout=60, check=False)
            final = ((account.stdout or "").split() or ["UNKNOWN"])[0]
            return False, final.rstrip("+")
        return False, "UNKNOWN"

    def project_state(self, project_id: str) -> dict:
        result = self._run([self.config.executable, "project-status", "--project-id", project_id, "--json"],
                           cwd=self.workspace, capture_output=True, text=True, timeout=300, check=False)
        try:
            return json.loads(result.stdout).get("state", {}) if result.returncode == 0 else {}
        except ValueError:
            return {}

    def poll(self) -> list[RunUpdate]:
        """Check running RAFT runs and record any that finished."""
        updates = []
        for run in self.registry.workflow_runs():
            if run.get("analysis_type") != "raft" or run["status"] != "running":
                continue
            parameters = json.loads(run["parameters_json"] or "{}")
            if parameters.get("auto"):
                continue  # per-patient projects are polled by pipelines.batches
            project_id = parameters.get("project_id", "")
            result = self.outcome(run.get("scheduler_job_id") or "", project_id)
            if result is None:
                continue
            status, detail = result
            state = {"detail": detail}
            self.registry.update_workflow_run(run["run_id"], status,
                                              error_message=None if status == "completed" else detail)
            if status == "completed":
                self.registry.register_output_artifact(
                    run["run_id"], "raft_project", str(self.workspace / "projects" / project_id),
                    metadata={"raft_state": state},
                )
            updates.append(RunUpdate(
                run_id=run["run_id"], project_id=project_id, workflow_name=run.get("workflow_name", ""),
                status=status, detail=detail, requested_by=run["requested_by"],
                channel_id=parameters.get("channel_id", ""),
            ))
        return updates
