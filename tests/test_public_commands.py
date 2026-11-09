import logging
from pathlib import Path
from unittest.mock import Mock

from instance import config as instance_config
from listeners.commands.status import handle_status_command
from registry import RegistryStatus


def _status():
    return RegistryStatus(1, 2, (Path("/x/IGI-MP-P101-UNC316_R1.fastq.gz"),), ())


def _run(monkeypatch):
    monkeypatch.setattr(
        "listeners.commands.status.RegistryService.from_environment",
        lambda: Mock(status=_status),
    )
    ack, respond, client = Mock(), Mock(), Mock()
    handle_status_command(ack=ack, respond=respond, body={"user_id": "U1", "channel_id": "C1"},
                          client=client, logger=logging.getLogger(__name__))
    ack.assert_called_once()
    return respond, client


def test_status_responds_in_channel(monkeypatch):
    respond, client = _run(monkeypatch)
    assert respond.call_args.kwargs["response_type"] == "in_channel"
    assert "IGI-MP-P101-UNC316_R1.fastq.gz" in str(respond.call_args.kwargs["blocks"])
    client.chat_postMessage.assert_not_called()


def test_status_lists_file_names_in_channel_even_when_protecting_phi(monkeypatch, test_instance):
    test_instance.write_text(test_instance.read_text() + "\n[privacy]\nprotect_phi = true\n")
    instance_config.clear_cache()
    respond, client = _run(monkeypatch)
    assert respond.call_args.kwargs["response_type"] == "in_channel"
    assert "IGI-MP-P101-UNC316_R1.fastq.gz" in str(respond.call_args.kwargs["blocks"])
    client.chat_postMessage.assert_not_called()
