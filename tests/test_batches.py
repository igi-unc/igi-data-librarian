import csv
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from instance import config as instance_config
from instance import parse_instance
from pipelines import PatientBatcher, RaftRunner
from pipelines.jobs import describe_batch, describe_queued
from registry import RegistryService

INSTANCE = """
[registry]
dir = "{registry}"
[vocabulary]
source = []
[privacy]
protect_phi = true
[permissions]
approvers = ["UAPPROVER"]
[raft]
workspace = "{workspace}"
executable = "/opt/raft"
launcher = "sbatch"
[[raft.workflows]]
id = "qc"
name = "Quick QC"
workflow = "qc"
version = "v1.0"
input_data = "multi"
workflow_profile = "standard"
auto_run = true
auto_delay_minutes = 15
"""

T0 = datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)


class FakeRun:
    def __init__(self):
        self.commands = []
        self.outputs = {}

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        key = command[0] if command[0] != "/opt/raft" else command[1]
        return subprocess.CompletedProcess(command, 0, stdout=self.outputs.get(key, ""), stderr="")

    def sbatch_scripts(self):
        return [command[-1] for command in self.commands if command[0] == "sbatch"]


@pytest.fixture
def env(tmp_path, test_instance):
    workspace = tmp_path / "raft"
    workspace.mkdir()
    (workspace / ".raft.cfg").write_text("{}")
    test_instance.write_text(test_instance.read_text()
                             + INSTANCE.format(registry=tmp_path / "registry", workspace=workspace))
    instance_config.clear_cache()
    service = RegistryService.from_environment()
    service.initialize()
    fake = FakeRun()
    batcher = PatientBatcher(RaftRunner(instance_config.get_instance(), service, run=fake))

    def accession(patient, lims_number, sample_type="tumor"):
        files = [service.sequencing_dir / f"IGI-{patient}-{lims_number}_R{n}.fastq.gz" for n in (1, 2)]
        for path in files:
            path.write_bytes(b"x")
        row, paths, _ = service.propose({
            "source": patient, "sample_type": sample_type, "analyte": "dna", "platform": "illumina",
            "library_type": "wgs", "species": "human", "sequenced_year": "2026",
            "sample_id": f"IGI-{patient}-{lims_number}", "lims_number": lims_number,
        })
        return service.commit(row, [str(p) for p in paths], "UACC")

    return batcher, fake, workspace, accession


def _manifest(workspace, project_id):
    with open(workspace / "inputs" / "metadata" / f"{project_id}.manifest.tsv", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def test_first_sample_starts_the_timer_and_later_samples_join_it(env):
    batcher, fake, _, accession = env
    [first] = batcher.enqueue(accession("P101", "000000001"), "UACC", "C1", now=T0)
    assert first.project_id == "test-qc-p101" and first.started_timer
    assert first.due_at == T0 + timedelta(minutes=15)

    [second] = batcher.enqueue(accession("P101", "000000002", "normal-blood"), "UACC", "C1",
                               now=T0 + timedelta(minutes=10))
    assert not second.started_timer and second.due_at == first.due_at  # not extended

    [other] = batcher.enqueue(accession("P102", "000000003"), "UACC", "C1", now=T0 + timedelta(minutes=10))
    assert other.project_id == "test-qc-p102" and other.started_timer

    assert batcher.dispatch_due(now=T0 + timedelta(minutes=14)) == []
    assert not fake.commands


def test_due_project_runs_all_patient_samples_in_one_job(env):
    batcher, fake, workspace, accession = env
    batcher.enqueue(accession("P101", "000000001"), "UACC", "C1", now=T0)
    batcher.enqueue(accession("P101", "000000002", "normal-blood"), "UACC", "C1", now=T0)
    fake.outputs["sbatch"] = "555\n"

    [update] = batcher.dispatch_due(now=T0 + timedelta(minutes=15))
    assert update.status == "started" and update.samples == 2 and update.new_samples == 2

    rows = _manifest(workspace, "test-qc-p101")
    assert [row["Run_Name"] for row in rows] == ["ad-000000001", "nd-000000002"]
    assert {row["Patient_Name"] for row in rows} == {"P101"} and rows[1]["Normal"] == "TRUE"
    staged = sorted(p.name for p in (workspace / "inputs" / "fastqs" / "test-qc-p101").iterdir())
    assert staged[0].startswith("ad-000000001_") and len(staged) == 4

    assert fake.commands[0][:2] == ["/opt/raft", "check-manifest"]
    [script] = fake.sbatch_scripts()
    steps = script.split(" && ")
    assert "--setup-only" in steps[0] and "--version v1.0" in steps[0]  # new project
    assert steps[1].startswith("/opt/raft update-mounts --project-id test-qc-p101")
    assert steps[2] == "/opt/raft run --project-id test-qc-p101"

    runs = batcher.registry.project_runs("test-qc-p101")
    assert {run["status"] for run in runs} == {"running"}
    assert batcher.registry.raft_project("test-qc-p101")["status"] == "running"
    assert batcher.dispatch_due(now=T0 + timedelta(hours=1)) == []  # nothing new, already running


def test_sample_arriving_during_a_run_goes_in_the_next_run(env):
    batcher, fake, workspace, accession = env
    batcher.enqueue(accession("P101", "000000001"), "UACC", "C1", now=T0)
    fake.outputs["sbatch"] = "555"
    batcher.dispatch_due(now=T0 + timedelta(minutes=15))
    (workspace / "projects" / "test-qc-p101").mkdir(parents=True)  # RAFT created the project

    [late] = batcher.enqueue(accession("P101", "000000002"), "ULATE", "C1", now=T0 + timedelta(minutes=20))
    assert late.started_timer and late.project_running
    assert batcher.dispatch_due(now=T0 + timedelta(minutes=40)) == []  # first run still going

    fake.outputs.update({"squeue": "RUNNING"})
    assert batcher.poll() == []
    fake.outputs.update({"squeue": "", "sacct": "COMPLETED",
                         "project-status": json.dumps({"state": {"id": "reports_available",
                                                                 "label": "reports available"}})})
    [done] = batcher.poll(now=T0 + timedelta(minutes=50))
    assert done.status == "completed" and done.new_samples == 1 and done.requested_by == ("UACC",)
    statuses = {run["parameters"]["run_name"]: run["status"] for run in batcher.registry.project_runs("test-qc-p101")}
    assert statuses == {"ad-000000001": "completed", "ad-000000002": "requested"}

    fake.outputs["sbatch"] = "556"
    [again] = batcher.dispatch_due(now=T0 + timedelta(minutes=50))
    assert again.samples == 2 and again.new_samples == 1 and again.requested_by == ("ULATE",)
    assert len(_manifest(workspace, "test-qc-p101")) == 2
    script = fake.sbatch_scripts()[-1]
    assert "--setup-only" not in script and script.endswith("/opt/raft run --project-id test-qc-p101")


def test_same_dataset_is_not_queued_twice_and_failures_are_reported(env):
    batcher, fake, workspace, accession = env
    dataset = accession("P101", "000000001")
    assert batcher.enqueue(dataset, "UACC", "C1", now=T0)
    assert batcher.enqueue(dataset, "UACC", "C1", now=T0) == []

    for path in batcher.registry.dataset(dataset)["files"]:
        __import__("os").remove(path)
    [failed] = batcher.dispatch_due(now=T0 + timedelta(minutes=15))
    assert failed.status == "failed" and "missing" in failed.detail
    assert batcher.registry.project_runs("test-qc-p101")[0]["status"] == "failed"
    assert batcher.registry.raft_project("test-qc-p101")["due_at"] is None
    assert not fake.sbatch_scripts()


def test_messages_are_channel_safe(env):
    batcher, fake, _, accession = env
    [queued] = batcher.enqueue(accession("P101", "000000001"), "UACC", "C1", now=T0)
    text = describe_queued(queued)
    assert "test-qc-p101" in text and "P101" in text and "<!date^" in text
    assert "IGI-P101-000000001" not in text
    fake.outputs["sbatch"] = "9"
    [started] = batcher.dispatch_due(now=T0 + timedelta(minutes=15))
    assert "1 sample(s), 1 new" in describe_batch(started)


def test_auto_delay_must_be_non_negative():
    with pytest.raises(ValueError, match="auto_delay_minutes"):
        parse_instance({"name": "X", "command_prefix": "x", "raft": {"workspace": "/w", "workflows": [
            {"id": "qc", "version": "v1", "auto_run": True, "auto_delay_minutes": -5}]}})
    config = parse_instance({"name": "X", "command_prefix": "x", "raft": {"workspace": "/w", "workflows": [
        {"id": "qc", "version": "v1", "auto_run": True}]}})
    assert [w.id for w in config.raft.auto_workflows] == ["qc"]
    assert config.raft.workflows[0].auto_delay_minutes == 15


def test_accession_in_slack_queues_qc_and_says_so(env, monkeypatch):
    import logging
    from unittest.mock import Mock

    from listeners.accession_state import PROPOSALS
    from listeners.actions import accession as accession_actions
    from pipelines import jobs

    batcher, _, _, _ = env
    service = batcher.registry
    monkeypatch.setattr(jobs, "runner", lambda: batcher.runner)
    files = [service.sequencing_dir / f"IGI-P101-000000009_R{n}.fastq.gz" for n in (1, 2)]
    for path in files:
        path.write_bytes(b"x")
    row, paths, _ = service.propose({
        "source": "P101", "sample_type": "tumor", "analyte": "dna", "platform": "illumina",
        "library_type": "wgs", "species": "human", "sequenced_year": "2026",
        "sample_id": "IGI-P101-000000009", "lims_number": "000000009",
    })
    PROPOSALS["p1"] = {"row": row, "files": [str(p) for p in paths], "user_id": "UACC", "channel_id": "C1"}
    slack = Mock()
    accession_actions.handle_accession_approve(
        ack=Mock(), body={"actions": [{"value": "p1"}], "user": {"id": "UACC"}, "view": {"id": "V", "hash": "h"}},
        client=slack, logger=logging.getLogger(__name__))
    channel = slack.chat_postMessage.call_args.kwargs["text"]
    assert "Quick QC for P101 (`test-qc-p101`) starts at" in channel
    assert service.raft_project("test-qc-p101")["due_at"]


def test_multi_input_stages_fastqs_and_bams_and_writes_lens_columns(env):
    batcher, fake, workspace, accession = env
    dataset = accession("P101", "000000001")
    bam = batcher.registry.sequencing_dir / "IGI-P101-000000005.bam"
    bam.write_bytes(b"x")
    service = batcher.registry
    row, paths, _ = service.propose({
        "source": "P101", "sample_type": "tumor", "analyte": "dna", "platform": "ont",
        "library_type": "wgs", "species": "human", "sequenced_year": "2026",
        "sample_id": "IGI-P101-000000005", "lims_number": "000000005",
    }, selected_files=[str(bam)])
    long_read = service.commit(row, [str(p) for p in paths], "UACC")
    batcher.enqueue(dataset, "UACC", "C1", now=T0)
    batcher.enqueue(long_read, "UACC", "C1", now=T0)
    fake.outputs["sbatch"] = "1"
    batcher.dispatch_due(now=T0 + timedelta(minutes=15))

    assert [p.name for p in (workspace / "inputs" / "bams" / "test-qc-p101").iterdir()] == [
        "ad-000000005_IGI-P101-000000005.bam"]
    assert len(list((workspace / "inputs" / "fastqs" / "test-qc-p101").iterdir())) == 2
    rows = {row["Run_Name"]: row for row in _manifest(workspace, "test-qc-p101")}
    assert rows["ad-000000005"]["Platform"] == "ONT" and rows["ad-000000005"]["Read_Type"] == "long-read"
    assert rows["ad-000000001"]["Platform"] == "Illumina" and rows["ad-000000001"]["Sample_Type"] == "tumor"
    script = fake.sbatch_scripts()[0]
    assert "--input-data multi" in script and "--workflow-profile standard" in script
    assert script.count("update-mounts") == 2  # fastqs/ and bams/
