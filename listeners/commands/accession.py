from logging import Logger
import uuid

from slack_bolt import Ack
from slack_sdk import WebClient

from listeners.views.accession_builder import build_accession_modal
from listeners.accession_state import SESSIONS
from registry import RegistryService


def handle_accession_command(ack: Ack, body: dict, client: WebClient, logger: Logger):
    ack()
    try:
        status = RegistryService.from_environment().status()
        if not status.unregistered:
            client.chat_postEphemeral(
                channel=body["channel_id"], user=body["user_id"],
                text="There are no unregistered files in the sequencing directory.",
            )
            return
        session_id = str(uuid.uuid4())
        SESSIONS[session_id] = {
            "files": [str(path) for path in status.unregistered],
            "user_id": body["user_id"],
            "channel_id": body["channel_id"],
        }
        client.views_open(
            trigger_id=body["trigger_id"],
            view=build_accession_modal(
                status.unregistered,
                vocabularies=RegistryService.from_environment().vocabularies(),
                workflows=RegistryService.from_environment().workflow_definitions(),
                session_id=session_id,
                channel_id=body["channel_id"],
                user_id=body["user_id"],
            ),
        )
        client.chat_postMessage(
            channel=body["channel_id"],
            text=(
                f"<@{body['user_id']}> opened the sequencing accession form with "
                f"{len(status.unregistered)} available unregistered file(s)."
            ),
        )
    except Exception as exc:
        logger.exception("Could not open accession form")
        client.chat_postEphemeral(
            channel=body["channel_id"], user=body["user_id"],
            text=f":warning: Could not open accession form: {exc}",
        )
