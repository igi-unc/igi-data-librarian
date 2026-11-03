import json
from pathlib import Path

import pytest

from instance import config as instance_config
from instance import load_instance, parse_instance
from instance.manifest import build_manifest
from listeners.commands.help import build_help_text
from registry import RegistryService

ROOT = Path(__file__).resolve().parent.parent


def test_pirl_example_instance_keeps_its_commands_and_files():
    pirl = load_instance(ROOT / "instances" / "pirl.toml")
    assert pirl.name == "PIRL Data Librarian"
    assert pirl.commands == {
        "status": "/pirl-status",
        "sequencing-accession": "/pirl-sequencing-accession",
        "reference-accession": "/pirl-reference-accession",
        "help": "/pirl-help",
    }
    service = RegistryService.from_instance(pirl)
    assert service.tsv_path.name == "pirl_sequencing.tsv"
    assert service.references_tsv_path.name == "pirl_references.tsv"
    assert service.db_path.name == "pirl_registry.sqlite3"
    assert service.id_prefix == "pirl"


def test_igi_instance_uses_its_own_commands():
    igi = load_instance(ROOT / "instances" / "igi.toml")
    assert igi.name == "IGI Data Librarian"
    assert igi.commands == {
        "status": "/igi-status",
        "sequencing-accession": "/igi-sequencing-accession",
        "reference-accession": "/igi-reference-accession",
        "help": "/igi-help",
    }
    assert igi.id_prefix == "igi"
    with pytest.raises(RuntimeError, match="No registry directory"):
        RegistryService.from_instance(igi)


def test_committed_manifest_matches_default_igi_instance():
    committed = json.loads((ROOT / "manifest.json").read_text())
    assert committed == build_manifest(load_instance(ROOT / "instances" / "igi.toml"))


def test_manifest_commands_follow_instance():
    manifest = build_manifest(parse_instance({"name": "IGI Data Librarian", "command_prefix": "igi"}))
    assert manifest["display_information"]["name"] == "IGI Data Librarian"
    assert [c["command"] for c in manifest["features"]["slash_commands"]] == [
        "/igi-status", "/igi-sequencing-accession", "/igi-reference-accession", "/igi-help",
    ]


def test_command_overrides_and_defaults():
    config = parse_instance({
        "name": "IGI Data Librarian",
        "command_prefix": "igi",
        "commands": {"status": "/igi-whatsnew"},
    })
    assert config.label == "IGI"
    assert config.id_prefix == "igi"
    assert config.bot_display_name == "igi-data-librarian"
    assert config.command("status") == "/igi-whatsnew"
    assert config.command("help") == "/igi-help"


@pytest.mark.parametrize("data, message", [
    ({"command_prefix": "igi"}, "'name' is required"),
    ({"name": "X", "command_prefix": "IGI"}, "command_prefix"),
    ({"name": "X", "command_prefix": "igi", "commands": {"stats": "/igi-stats"}}, "Unknown"),
    ({"name": "X", "command_prefix": "igi", "commands": {"status": "igi-status"}}, "Invalid slash command"),
    ({"name": "X", "command_prefix": "igi", "commands": {"status": "/igi-help"}}, "distinct"),
    ({"name": "X", "command_prefix": "a-very-long-instance-prefix"}, "at most 32"),
])
def test_invalid_configs_are_rejected(data, message):
    with pytest.raises(ValueError, match=message):
        parse_instance(data)


def test_registry_dir_env_overrides_config(tmp_path, monkeypatch):
    monkeypatch.setenv(instance_config.REGISTRY_DIR_ENV, str(tmp_path / "elsewhere"))
    config = load_instance(ROOT / "instances" / "igi.toml")
    assert config.registry_dir == tmp_path / "elsewhere"


def test_registry_ids_use_instance_prefix(tmp_path):
    service = RegistryService(tmp_path, id_prefix="igi")
    service.initialize()
    (tmp_path / "sequencing" / "sample_R1.fastq.gz").write_text("@r\nA\n+\n!\n")
    row, _, _ = service.propose({"source": "lab", "sample_type": "tumor", "analyte": "dna"})
    assert row["uuid"].startswith("igi_lab_tumor_dna")


def test_missing_instance_env_is_explained(monkeypatch):
    monkeypatch.delenv(instance_config.INSTANCE_ENV)
    instance_config.clear_cache()
    with pytest.raises(RuntimeError, match="DATA_LIBRARIAN_INSTANCE"):
        instance_config.get_instance()


def test_help_uses_active_instance():
    text = build_help_text()
    assert "Test Librarian" in text
    assert "/test-sequencing-accession" in text
