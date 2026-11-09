import csv
import json
import logging
import subprocess
from unittest.mock import Mock

import pytest

from instance import config as instance_config
from instance import parse_instance
from listeners.actions import raft as raft_actions
from listeners.commands import run as run_command
from pipelines import RaftError, RaftRunner
from pipelines.raft import manifest_row
from registry import HEADERS, RegistryService
from registry.service import LEGACY_HEADERS

RAFT_INSTANCE = """
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
sbatch_args = ["--time=1:00:00"]
[[raft.workflows]]
id = "qc"
name = "Sequencing QC"
workflow = "qc"
version = "v1.0"
input_data = "fastqs"
[[raft.workflows]]
id = "later"
workflow = "lens"
enabled = false
"""


class FakeRun:
    """Stands in for subprocess.run and records each command."""

    def __init__(self):
        self.commands = []
        self.outputs = {}

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        key = command[0] if command[0] != "/opt/raft" else command[1]
        stdout = self.outputs.get(key, "")
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


@pytest.fixture
def raft_env(tmp_path, test_instance):
    workspace = tmp_path / "raft"
    workspace.mkdir()
    (workspace / ".raft.cfg").write_text("{}")
    registry_dir = tmp_path / "registry"
    test_instance.write_text(test_instance.read_text()
                             + RAFT_INSTANCE.format(registry=registry_dir, workspace=workspace))
    instance_config.clear_cache()
    service = RegistryService.from_environment()
    service.initialize()
    files = [service.sequencing_dir / f"IGI-SAMPLE-UNC316_R{n}.fastq.gz" for n in (1, 2)]
    for path in files:
        path.write_bytes(b"x")
    row, paths, _ = service.propose({
        "source": "P101", "sample_type": "tumor", "analyte": "dna", "platform": "illumina",
        "library_type": "wgs", "species": "human", "sequenced_year": "2026",
        "sample_id": "IGI-SAMPLE-UNC316", "lims_number": "000000042",
    })
    internal_id = service.commit(row, [str(p) for p in paths], "UAPPROVER")
    fake = FakeRun()
    runner = RaftRunner(instance_config.get_instance(), service, run=fake)
    return runner, fake, internal_id, workspace, files


def test_raft_config_parsing():
    config = parse_instance({"name": "X", "command_prefix": "x"})
    assert "run" not in config.commands and config.raft is None
    with pytest.raises(ValueError, match="no version"):
        parse_instance({"name": "X", "command_prefix": "x",
                        "raft": {"workspace": "/w", "workflows": [{"id": "qc"}]}})
    with pytest.raises(ValueError, match="launcher"):
        parse_instance({"name": "X", "command_prefix": "x", "raft": {"workspace": "/w", "launcher": "ssh"}})


def test_manifest_row_maps_registry_fields():
    dataset = {"internal_id": "abcd-1234", "row": {
        "source": "P101", "sample_type": "normal-blood", "analyte": "rna", "library_type": "poly-a",
        "platform": "ont", "lims_number": "000000042"}}
    row = manifest_row(dataset, "igi-qc-1234abcd")
    assert row == {"Patient_Name": "P101", "Run_Name": "nr-000000042", "Dataset": "igi-qc-1234abcd",
                   "File_Prefix": "nr-000000042", "Sequencing_Method": "RNA-Seq", "Normal": "TRUE",
                   "Group": "", "Platform": "ONT", "Read_Type": "long-read", "Sample_Type": "blood-normal"}


def test_prepare_symlinks_inputs_and_runs_setup_only(raft_env):
    runner, fake, internal_id, workspace, files = raft_env
    run_id, project_id = runner.request("qc", internal_id, "UAPPROVER", "C1")
    assert project_id.startswith("test-qc-") and "unc" not in project_id.lower()
    result = runner.prepare(run_id)

    staged = sorted((workspace / "inputs" / "fastqs" / project_id).iterdir())
    assert [p.name for p in staged] == [f"ad-000000042_{f.name}" for f in files]
    assert all(p.is_symlink() and p.resolve() == f.resolve() for p, f in zip(staged, files))
    assert all(f.exists() for f in files)  # originals untouched

    with open(result["manifest"], newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    assert rows[0]["File_Prefix"] == "ad-000000042" and rows[0]["Dataset"] == project_id
    assert rows[0]["Sequencing_Method"] == "WGS" and rows[0]["Normal"] == "FALSE"

    check, setup = fake.commands
    assert check[:2] == ["/opt/raft", "check-manifest"]
    assert setup[:4] == ["/opt/raft", "run", "--project-id", project_id]
    assert "--setup-only" in setup and setup[setup.index("--version") + 1] == "v1.0"
    assert runner.registry.workflow_run(run_id)["status"] == "ready"
    runner.prepare(run_id)  # re-running is idempotent for existing links


def test_launch_requires_approver_then_submits_with_sbatch(raft_env):
    runner, fake, internal_id, workspace, _ = raft_env
    run_id, project_id = runner.request("qc", internal_id, "UAPPROVER", "C1")
    runner.prepare(run_id)
    with pytest.raises(PermissionError):
        runner.launch(run_id, approved_by="USOMEONE")
    fake.outputs["sbatch"] = "12345\n"
    assert runner.launch(run_id, approved_by="UAPPROVER") == "slurm:12345"
    sbatch = fake.commands[-1]
    assert sbatch[0] == "sbatch" and "--time=1:00:00" in sbatch
    assert sbatch[-1] == f"/opt/raft run --project-id {project_id}"
    with pytest.raises(RaftError, match="running"):
        runner.launch(run_id, approved_by="UAPPROVER")


def test_poll_records_completion(raft_env):
    runner, fake, internal_id, _, _ = raft_env
    run_id, project_id = runner.request("qc", internal_id, "UAPPROVER", "C1")
    runner.prepare(run_id)
    fake.outputs["sbatch"] = "777"
    runner.launch(run_id, approved_by="UAPPROVER")

    fake.outputs["squeue"] = "RUNNING\n"
    assert runner.poll() == []

    fake.outputs.update({"squeue": "", "sacct": "COMPLETED \n",
                         "project-status": json.dumps({"state": {"id": "reports_available",
                                                                 "label": "complete, reports available"}})})
    [update] = runner.poll()
    assert update.status == "completed" and update.project_id == project_id and update.channel_id == "C1"
    assert runner.registry.workflow_run(run_id)["status"] == "completed"
    assert runner.poll() == []


def test_disabled_workflow_and_non_fastq_dataset_are_refused(raft_env):
    runner, _, internal_id, _, _ = raft_env
    with pytest.raises(RaftError, match="not enabled"):
        runner.request("later", internal_id, "UAPPROVER", "C1")


def test_run_command_is_approver_only(raft_env, monkeypatch):
    runner, *_ = raft_env
    monkeypatch.setattr(run_command, "runner", lambda: runner)
    client = Mock()
    body = {"user_id": "UOTHER", "channel_id": "C1", "trigger_id": "t"}
    run_command.handle_run_command(ack=Mock(), body=body, client=client, logger=logging.getLogger(__name__))
    assert "Only approvers" in client.chat_postEphemeral.call_args.kwargs["text"]
    client.views_open.assert_not_called()

    body["user_id"] = "UAPPROVER"
    run_command.handle_run_command(ack=Mock(), body=body, client=client, logger=logging.getLogger(__name__))
    view = client.views_open.call_args.kwargs["view"]
    assert view["callback_id"] == "raft_run_submit"


def test_launch_button_posts_only_project_id_to_channel(raft_env, monkeypatch):
    runner, fake, internal_id, _, _ = raft_env
    run_id, project_id = runner.request("qc", internal_id, "UAPPROVER", "C1")
    runner.prepare(run_id)
    fake.outputs["sbatch"] = "99"
    monkeypatch.setattr(raft_actions, "runner", lambda: runner)
    client = Mock()
    body = {"user": {"id": "UAPPROVER"}, "actions": [{"value": run_id}], "channel": {"id": "DUAPP"},
            "message": {"ts": "1.0", "blocks": [{"type": "section"}, {"type": "actions"}]}}
    raft_actions.handle_raft_launch(ack=Mock(), body=body, client=client, logger=logging.getLogger(__name__))
    channel_post = client.chat_postMessage.call_args.kwargs
    assert channel_post["channel"] == "C1" and project_id in channel_post["text"]
    assert "UNC316" not in channel_post["text"]
    updated = client.chat_update.call_args.kwargs
    assert all(block["type"] != "actions" for block in updated["blocks"])


# --- registry changes ---------------------------------------------------------------

def test_legacy_tsv_gains_lims_number_column(tmp_path):
    service = RegistryService(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    with service.tsv_path.open("w", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(LEGACY_HEADERS)
        writer.writerow(["old_id"] + [""] * (len(LEGACY_HEADERS) - 1))
    service.initialize()
    with service.tsv_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    assert list(rows[0]) == HEADERS and rows[0]["uuid"] == "old_id" and rows[0]["lims_number"] == ""


def test_vocabulary_override_replaces_seeded_defaults(raft_env):
    runner, *_ = raft_env
    sources = runner.registry.vocabularies()["source"]
    assert "u87" not in sources and "P101" in sources  # PIRL cell lines gone; used value kept
