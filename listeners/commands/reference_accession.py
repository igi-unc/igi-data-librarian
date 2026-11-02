import uuid
from logging import Logger

from slack_bolt import Ack
from slack_sdk import WebClient

from listeners.accession_state import SESSIONS
from listeners.views.reference_accession_builder import build_reference_accession_modal
from registry import RegistryService


def handle_reference_accession_command(ack: Ack, body: dict, client: WebClient, logger: Logger):
    ack()
    try:
        service = RegistryService.from_environment()
        files = service.status().unregistered_references
        if not files:
            client.chat_postEphemeral(channel=body["channel_id"], user=body["user_id"],
                                     text="There are no unregistered files in the references directory.")
            return
        session_id = str(uuid.uuid4())
        SESSIONS[session_id] = {"files": [str(path) for path in files], "user_id": body["user_id"],
                                "channel_id": body["channel_id"], "kind": "reference"}
        client.views_open(trigger_id=body["trigger_id"], view=build_reference_accession_modal(
            files, service.reference_vocabularies(), session_id, body["channel_id"], body["user_id"]
        ))
        client.chat_postMessage(channel=body["channel_id"], text=(
            f"<@{body['user_id']}> opened the reference accession form with {len(files)} available unregistered file(s)."
        ))
    except Exception as exc:
        logger.exception("Could not open reference accession form")
        client.chat_postEphemeral(channel=body["channel_id"], user=body["user_id"],
                                 text=f":warning: Could not open reference accession form: {exc}")
