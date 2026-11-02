from pathlib import Path

from listeners.views.accession_builder import build_accession_modal, build_review_view


def test_review_includes_approve_and_next():
    row = {"uuid": "test-dataset", "notes": ""}
    view = build_review_view("proposal", row, [str(Path("one.fastq.gz"))], [])
    action_ids = [
        element["action_id"]
        for block in view["blocks"] if block["type"] == "actions"
        for element in block["elements"]
    ]
    assert "accession_approve_next" in action_ids


def test_accession_uses_visible_file_checkboxes_and_single_controlled_inputs():
    files = [Path(f"sample_{index}.fastq.gz") for index in range(12)]
    view = build_accession_modal(files)
    file_blocks = [
        block for block in view["blocks"]
        if block.get("block_id", "").startswith("selected_files_")
    ]
    assert len(file_blocks) == 2
    assert all(block["element"]["type"] == "checkboxes" for block in file_blocks)
    assert not any("__new" in block.get("block_id", "") for block in view["blocks"])
    source = next(block for block in view["blocks"] if block.get("block_id") == "source")
    assert source["dispatch_action"] is True
    assert source["element"]["action_id"] == "controlled_select__source"


def test_accession_can_request_workflows():
    workflows = [{
        "workflow_id": "raft_hla_typing", "name": "HLA typing",
        "description": "Infer HLA alleles.",
    }]
    view = build_accession_modal([Path("sample.fastq.gz")], workflows=workflows)
    block = next(item for item in view["blocks"] if item.get("block_id") == "requested_workflows")
    assert block["optional"] is True
    assert block["element"]["options"][0]["value"] == "raft_hla_typing"
    hla = next(item for item in view["blocks"] if item.get("block_id") == "known_hla_alleles")
    assert hla["optional"] is True
    assert hla["element"]["multiline"] is True
