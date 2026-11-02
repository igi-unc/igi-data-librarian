import csv
from pathlib import Path

from registry import HEADERS, REFERENCE_HEADERS, RegistryService


def test_registry_status_and_commit(tmp_path: Path):
    service = RegistryService(tmp_path)
    service.initialize()
    dropped = service.drop_dir / "sample_R1.fastq.gz"
    dropped.write_bytes(b"test")
    status = service.status()
    assert status.datasets == 0
    assert status.unregistered == (dropped.resolve(),)

    values = {
        "source": "u87", "sample_type": "cell-line", "analyte": "rna",
        "platform": "illumina", "library_type": "poly-a", "species": "human",
        "sequenced_year": "2026", "sequencing_read_type": "single-end",
    }
    row, files, warnings = service.propose(values)
    assert row["uuid"] == "data_u87_cellline_rna_illumina_polya_human_2026"
    assert not warnings
    service.commit(row, [str(path) for path in files], "U123")

    assert service.status().datasets == 1
    assert not service.status().unregistered
    with service.tsv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    assert list(rows[0]) == HEADERS
    assert rows[0]["source"] == "u87"


def test_commit_rejects_stale_files(tmp_path: Path):
    service = RegistryService(tmp_path)
    service.initialize()
    row = {header: "" for header in HEADERS}
    row["uuid"] = "test-dataset"
    try:
        service.commit(row, [str(tmp_path / "not-dropped.fastq.gz")], "U123")
    except ValueError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("stale proposal was committed")


def test_proposal_normalizes_blank_slack_values(tmp_path: Path):
    service = RegistryService(tmp_path)
    service.initialize()
    (service.drop_dir / "sample.fastq.gz").write_bytes(b"test")
    values = {
        "source": "u87", "sample_type": "cell-line", "analyte": "rna",
        "platform": "illumina", "library_type": "poly-a", "species": "human",
        "sequenced_year": "2026", "sample_id": None, "notes": None,
    }
    row, _, _ = service.propose(values)
    assert row["sample_id"] == ""
    assert row["notes"] == ""


def test_approved_value_is_added_to_vocabulary(tmp_path: Path):
    service = RegistryService(tmp_path)
    service.initialize()
    dropped = service.drop_dir / "sample.fastq.gz"
    dropped.write_bytes(b"test")
    values = {
        "source": "new-source", "sample_type": "organoid", "analyte": "rna",
        "platform": "illumina", "library_type": "poly-a", "species": "human",
        "sequenced_year": "2026",
    }
    row, files, _ = service.propose(values)
    service.commit(row, [str(path) for path in files], "U123")
    assert "new-source" in service.vocabularies()["source"]
    assert "organoid" in service.vocabularies()["sample_type"]


def test_proposal_uses_selected_subset(tmp_path: Path):
    service = RegistryService(tmp_path)
    service.initialize()
    first = service.drop_dir / "first.fastq.gz"
    second = service.drop_dir / "second.fastq.gz"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    values = {
        "source": "u87", "sample_type": "cell-line", "analyte": "rna",
        "platform": "illumina", "library_type": "poly-a", "species": "human",
        "sequenced_year": "2026",
    }
    _, files, _ = service.propose(values, selected_files=[str(second)])
    assert files == (second.resolve(),)


def test_reference_status_proposal_and_commit(tmp_path: Path):
    service = RegistryService(tmp_path)
    service.initialize()
    fasta = service.references_dir / "GRCh38.fa.gz"
    annotation = service.references_dir / "gencode.v48.gtf.gz"
    fasta.write_bytes(b"fasta")
    annotation.write_bytes(b"gtf")

    status = service.status()
    assert status.unregistered_references == (fasta.resolve(), annotation.resolve())
    row, files, warnings = service.propose_reference(
        {"name": "GENCODE", "reference_type": "genome", "species": "human",
         "assembly": "GRCh38", "version": "48", "file_format": "fasta"},
        [str(fasta), str(annotation)],
    )
    assert row["reference_id"] == "data_ref_gencode_human_grch38_48_genome"
    assert not warnings
    service.commit_reference(row, [str(path) for path in files], "U123")
    assert service.status().reference_datasets == 1
    assert not service.status().unregistered_references
    with service.references_tsv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    assert list(rows[0]) == REFERENCE_HEADERS
    assert rows[0]["assembly"] == "GRCh38"


def test_workflows_are_requested_and_results_are_registered(tmp_path: Path):
    service = RegistryService(tmp_path)
    service.initialize()
    dropped = service.sequencing_dir / "sample_R1.fastq.gz"
    dropped.write_bytes(b"test")
    values = {
        "source": "u87", "sample_type": "cell-line", "analyte": "rna",
        "platform": "illumina", "library_type": "poly-a", "species": "human",
        "sequenced_year": "2026", "sequencing_read_type": "single-end",
    }
    row, files, _ = service.propose(values)
    dataset_id = service.commit(
        row, [str(path) for path in files], "U123",
        workflow_ids=["raft_hla_typing", "raft_gene_expression", "raft_fusion_detection"],
    )
    runs = service.workflow_runs(dataset_id)
    assert len(runs) == 3
    assert {run["status"] for run in runs} == {"requested"}

    hla_run = next(run for run in runs if run["analysis_type"] == "hla")
    service.update_workflow_run(hla_run["run_id"], "running", scheduler_job_id="12345")
    service.replace_result_rows(hla_run["run_id"], "hla", [{
        "gene": "HLA-A", "allele": "A*02:01", "resolution": "two-field",
        "confidence": 0.99, "caller": "test", "call_rank": 1,
    }])
    artifact_id = service.register_output_artifact(
        hla_run["run_id"], "hla_report", "gs://example-bucket/results/hla.json",
        format="json", row_count=1,
    )
    service.update_workflow_run(
        hla_run["run_id"], "completed", software_versions_json={"caller": "1.0"}
    )
    with service._connect() as connection:
        assert connection.execute("SELECT allele FROM hla_calls").fetchone()[0] == "A*02:01"
        assert connection.execute(
            "SELECT artifact_id FROM output_artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()[0] == artifact_id


def test_incompatible_workflow_is_rejected(tmp_path: Path):
    service = RegistryService(tmp_path)
    service.initialize()
    try:
        service.validate_workflows(
            ["raft_gene_expression"], {"analyte": "dna", "library_type": "wgs"}
        )
    except ValueError as exc:
        assert "not compatible" in str(exc)
    else:
        raise AssertionError("incompatible workflow was accepted")


def test_user_provided_hla_is_normalized_and_stored_with_provenance(tmp_path: Path):
    service = RegistryService(tmp_path)
    service.initialize()
    calls = service.parse_provided_hla("HLA-A*02:01, a*03:01\nB*07:02; HLA-A*02:01")
    assert [call["allele"] for call in calls] == ["A*02:01", "A*03:01", "B*07:02"]
    for call in calls:
        call["metadata_json"] = {"source_or_method": "clinical typing"}

    dropped = service.sequencing_dir / "sample.fastq.gz"
    dropped.write_bytes(b"test")
    values = {
        "source": "u87", "sample_type": "cell-line", "analyte": "dna",
        "platform": "illumina", "library_type": "wgs", "species": "human",
        "sequenced_year": "2026",
    }
    row, files, _ = service.propose(values)
    dataset_id = service.commit(
        row, [str(path) for path in files], "U123", provided_hla_calls=calls
    )
    runs = service.workflow_runs(dataset_id)
    assert len(runs) == 1
    assert runs[0]["workflow_id"] == "provided_hla"
    assert runs[0]["status"] == "completed"
    with service._connect() as connection:
        stored = connection.execute(
            "SELECT allele, metadata_json FROM hla_calls ORDER BY call_rank"
        ).fetchall()
    assert [item["allele"] for item in stored] == ["A*02:01", "A*03:01", "B*07:02"]
    assert "clinical typing" in stored[0]["metadata_json"]


def test_invalid_user_provided_hla_is_rejected(tmp_path: Path):
    service = RegistryService(tmp_path)
    try:
        service.parse_provided_hla("A02:01")
    except ValueError as exc:
        assert "Invalid HLA allele" in str(exc)
    else:
        raise AssertionError("invalid HLA allele was accepted")
