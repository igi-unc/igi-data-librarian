import json
import logging
import urllib.error
from unittest.mock import Mock

import pytest

from instance import LimsConfig
from instance import config as instance_config
from lims import LimsClient, LimsError
from listeners.accession_state import PROPOSALS, SESSIONS
from listeners.actions import accession as accession_actions
from listeners.views import accession_submission
from registry import RegistryService

PHI_SAMPLE_ID = "IGI-MP-P101-UNC316-26SEP29-PF-TUM-HMD-01-KEN-26OCT06"
FOUND = {
    "found": True, "lims_number": "42",
    "safe": {"participant_id": "P101", "sample_type": "tumor", "analyte": "dna", "platform": "ont",
             "library_type": "wgs", "species": "human", "unexpected": "dropped"},
    "phi": {"sample_id": PHI_SAMPLE_ID, "unc_id": "UNC316", "received": "2026-09-29"},
}


def _client(responses, monkeypatch=None):
    calls = []

    def opener(request, timeout):
        calls.append(request)
        status, body = responses.pop(0)
        if isinstance(status, Exception):
            raise status
        return status, json.dumps(body).encode() if not isinstance(body, bytes) else body

    config = LimsConfig(url="https://lims.example", read_token_env="T_READ", write_token_env="T_WRITE")
    return LimsClient(config, opener=opener), calls


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.setenv("T_READ", "read-token")
    monkeypatch.setenv("T_WRITE", "write-token")


def test_lookup_keeps_only_safe_fields():
    client, calls = _client([(200, FOUND)])
    tube = client.registry_fields(PHI_SAMPLE_ID)
    assert tube.found and tube.lims_number == "000000042"
    assert tube.safe == {"participant_id": "P101", "sample_type": "tumor", "analyte": "dna",
                         "platform": "ont", "library_type": "wgs", "species": "human"}
    assert "UNC316" not in repr(tube)
    assert tube.label == "LIMS #000000042 · P101"
    assert calls[0].get_method() == "GET"
    assert calls[0].full_url == f"https://lims.example/api/samples/{PHI_SAMPLE_ID}/registry-fields"
    assert calls[0].headers["X-api-token"] == "read-token"


def test_lookup_not_found_and_errors_never_echo_bodies():
    client, _ = _client([(404, {"found": False})])
    assert client.registry_fields("000000042").found is False

    client, _ = _client([(500, {"error": f"crash for {PHI_SAMPLE_ID}"})])
    with pytest.raises(LimsError) as error:
        client.registry_fields("000000042")
    assert "UNC316" not in str(error.value) and "HTTP 500" in str(error.value)

    client, _ = _client([(404, b"<html>no route</html>")])  # endpoint not built yet
    with pytest.raises(LimsError, match="not available"):
        client.registry_fields("000000042")

    client, _ = _client([(urllib.error.URLError("down"), None)])
    with pytest.raises(LimsError, match="could not be reached"):
        client.registry_fields("000000042")

    client, _ = _client([(401, {})])
    with pytest.raises(LimsError, match="read token"):
        client.registry_fields("000000042")


def test_lookup_rejects_odd_keys_and_missing_tokens(monkeypatch):
    client, calls = _client([])
    with pytest.raises(ValueError):
        client.registry_fields("../api/search?q=P1")
    monkeypatch.delenv("T_READ")
    with pytest.raises(LimsError, match="No LIMS token"):
        client.registry_fields("000000042")
    assert not calls


def test_link_dataset_posts_with_write_token():
    client, calls = _client([(201, {"status": "created"}), (200, {"status": "exists"})])
    kwargs = dict(registry_id="igi_p101", instance="igi", paths=["/d/a.fastq.gz"],
                  accessioned_by="U1", approved_by="U1")
    assert client.link_dataset("42", **kwargs) == "created"
    assert client.link_dataset("000000042", **kwargs) == "exists"
    assert calls[0].full_url == "https://lims.example/api/samples/000000042/files"
    assert calls[0].headers["X-api-token"] == "write-token"
    assert json.loads(calls[0].data)["registry_id"] == "igi_p101"


# --- accession flow ------------------------------------------------------------------

LIMS_INSTANCE = """
[registry]
id_fields = ["source", "lims_number", "sample_type", "analyte", "library_type", "species", "sequenced_year"]
[vocabulary]
source = []
[privacy]
protect_phi = true
[permissions]
approvers = ["UAPPROVER"]
[lims]
url = "https://lims.example"
read_token_env = "T_READ"
write_token_env = "T_WRITE"
"""


@pytest.fixture
def lims_setup(tmp_path, test_instance, monkeypatch):
    registry_dir = tmp_path / "registry"
    test_instance.write_text(test_instance.read_text() + f'\n{LIMS_INSTANCE}\n'
                             .replace("[registry]", f'[registry]\ndir = "{registry_dir}"'))
    instance_config.clear_cache()
    service = RegistryService.from_environment()
    service.initialize()
    fastq = service.sequencing_dir / f"{PHI_SAMPLE_ID}_R1.fastq.gz"
    fastq.write_bytes(b"x")
    fake = Mock()
    monkeypatch.setattr(accession_submission, "lims_client", lambda config: fake)
    monkeypatch.setattr(accession_actions, "lims_client", lambda config: fake)
    SESSIONS["s1"] = {"files": [str(fastq.resolve())], "user_id": "UAPPROVER", "channel_id": "C1"}
    yield service, fake, fastq
    SESSIONS.clear()
    PROPOSALS.clear()


def _submit(user="UAPPROVER", sample_id=PHI_SAMPLE_ID, write_back=True, extra=None):
    def select(value):
        return {"a": {"type": "static_select", "selected_option": {"value": value} if value else None}}

    state = {
        "selected_files_0": {"a": {"type": "checkboxes", "selected_options": [{"value": "0"}]}},
        "source": select(""), "sample_type": select(""), "analyte": select(""),
        "platform": select(""), "library_type": select(""), "species": select(""),
        "sequenced_year": {"a": {"type": "plain_text_input", "value": "2026"}},
        "sample_id": {"a": {"type": "plain_text_input", "value": sample_id}},
        "lims_write_back": {"a": {"type": "checkboxes",
                                  "selected_options": [{"value": "write_back"}] if write_back else []}},
    }
    state.update(extra or {})
    body = {"user": {"id": user}, "view": {"id": "V1", "state": {"values": state},
                                           "private_metadata": json.dumps({"session_id": "s1", "channel_id": "C1"})}}
    ack, client = Mock(), Mock()
    accession_submission.handle_accession_submission(ack=ack, body=body, client=client,
                                                     logger=logging.getLogger(__name__))
    return ack, client


def test_lims_fills_blank_fields_and_keeps_phi_out_of_channel(lims_setup):
    service, fake, fastq = lims_setup
    from lims import LimsTube
    fake.registry_fields.return_value = LimsTube(True, "000000042", {k: v for k, v in FOUND["safe"].items()
                                                                     if k != "unexpected"})
    fake.link_dataset.return_value = "created"
    ack, client = _submit()
    assert ack.call_args.kwargs["response_action"] == "update"  # "checking the LIMS" placeholder
    review = client.views_update.call_args.kwargs["view"]
    assert review["callback_id"] == "accession_review"
    assert "LIMS #000000042 · P101" in str(review)
    proposal_id, proposal = next(iter(PROPOSALS.items()))
    assert proposal["row"]["source"] == "P101" and proposal["row"]["lims_number"] == "000000042"
    assert "unc316" not in proposal["row"]["uuid"]  # dataset IDs no longer carry the sample ID
    assert proposal["lims_write_back"] is True

    slack = Mock()
    accession_actions.handle_accession_approve(
        ack=Mock(), body={"actions": [{"value": proposal_id}], "user": {"id": "UAPPROVER"},
                          "view": {"id": "V1", "hash": "h"}},
        client=slack, logger=logging.getLogger(__name__))
    fake.link_dataset.assert_called_once()
    assert fake.link_dataset.call_args.args[0] == "000000042"
    channel = slack.chat_postMessage.call_args.kwargs
    assert channel["channel"] == "C1"
    assert "LIMS #000000042 · P101" in channel["text"] and "Recorded in the LIMS" in channel["text"]
    assert "UNC316" not in channel["text"] and proposal["row"]["uuid"] not in channel["text"]
    assert service.status().datasets == 1


def test_unknown_tube_stops_accession(lims_setup):
    _, fake, _ = lims_setup
    from lims import LimsTube
    fake.registry_fields.return_value = LimsTube(False)
    _, client = _submit()
    assert "Nothing was registered" in str(client.views_update.call_args.kwargs["view"])
    assert not PROPOSALS


def test_non_approver_cannot_write_back(lims_setup):
    _, fake, _ = lims_setup
    from lims import LimsTube
    SESSIONS["s1"]["user_id"] = "UOTHER"
    fake.registry_fields.return_value = LimsTube(True, "000000042", {"participant_id": "P101",
        "sample_type": "tumor", "analyte": "dna", "platform": "ont", "library_type": "wgs", "species": "human"})
    _submit(user="UOTHER", write_back=True)
    proposal = next(iter(PROPOSALS.values()))
    assert proposal["lims_write_back"] is False


def test_without_sample_id_required_fields_are_checked_inline(lims_setup):
    _, fake, _ = lims_setup
    ack, _ = _submit(sample_id="")
    assert ack.call_args.kwargs["response_action"] == "errors"
    assert "LIMS" in ack.call_args.kwargs["errors"]["source"]
    fake.registry_fields.assert_not_called()
