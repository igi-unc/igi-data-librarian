import logging
from unittest.mock import Mock

from listeners.commands.status import handle_status_command
from registry import RegistryStatus


def test_status_responds_in_channel(monkeypatch):
    monkeypatch.setattr(
        "listeners.commands.status.RegistryService.from_environment",
        lambda: Mock(status=lambda: RegistryStatus(1, 2, (), ())),
    )
    ack = Mock()
    respond = Mock()
    handle_status_command(ack=ack, respond=respond, logger=logging.getLogger(__name__))
    ack.assert_called_once()
    assert respond.call_args.kwargs["response_type"] == "in_channel"
