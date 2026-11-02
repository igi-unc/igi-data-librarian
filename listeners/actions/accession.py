import uuid
from logging import Logger

from slack_bolt import Ack
from slack_sdk import WebClient

from listeners.accession_state import PROPOSALS, SESSIONS
from listeners.views.accession_builder import build_accession_modal
from listeners.views.reference_accession_builder import build_reference_accession_modal
from registry import RegistryService


def _message_view(title: str, message: str) -> dict:
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": title},
        "close": {"type": "plain_text", "text": "Done"},
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": message}}],
    }


def _take_proposal(body: dict) -> dict:
    proposal = PROPOSALS.pop(body["actions"][0]["value"], None)
    if not proposal or proposal["user_id"] != body["user"]["id"]:
        raise ValueError("This proposal expired or belongs to another user.")
    return proposal


def _commit(body: dict, client: WebClient) -> dict:
    proposal = _take_proposal(body)
    service = RegistryService.from_environment()
    if proposal.get("kind") == "reference":
        service.commit_reference(proposal["row"], proposal["files"], body["user"]["id"])
        accession_id = proposal["row"]["reference_id"]
    else:
        service.commit(
            proposal["row"], proposal["files"], body["user"]["id"],
            workflow_ids=proposal.get("workflow_ids", []),
            provided_hla_calls=proposal.get("provided_hla_calls", []),
        )
        accession_id = proposal["row"]["uuid"]
    if proposal.get("channel_id"):
        client.chat_postMessage(
            channel=proposal["channel_id"],
            text=(
                f":white_check_mark: <@{body['user']['id']}> accessioned "
                f"`{accession_id}` with {len(proposal['files'])} file(s)."
                + (f" Requested {len(proposal.get('workflow_ids', []))} analysis workflow(s)." if proposal.get("workflow_ids") else "")
                + (f" Recorded {len(proposal.get('provided_hla_calls', []))} known HLA allele(s)." if proposal.get("provided_hla_calls") else "")
            ),
        )
    return proposal


def handle_accession_approve(ack: Ack, body: dict, client: WebClient, logger: Logger):
    ack()
    try:
        proposal = _commit(body, client)
        client.views_update(
            view_id=body["view"]["id"], hash=body["view"].get("hash"),
            view=_message_view(
                "Accession complete",
                f":white_check_mark: Accessioned `{proposal['row'].get('uuid') or proposal['row']['reference_id']}` with {len(proposal['files'])} file(s).",
            ),
        )
    except Exception as exc:
        logger.exception("Accession commit failed")
        client.views_update(
            view_id=body["view"]["id"], hash=body["view"].get("hash"),
            view=_message_view("Accession failed", f":warning: {exc}"),
        )


def handle_accession_approve_next(
    ack: Ack, body: dict, client: WebClient, logger: Logger
):
    ack()
    try:
        proposal = _commit(body, client)
        service = RegistryService.from_environment()
        is_reference = proposal.get("kind") == "reference"
        remaining = service.status().unregistered_references if is_reference else service.status().unregistered
        accession_id = proposal["row"].get("uuid") or proposal["row"]["reference_id"]
        if not remaining:
            next_view = _message_view(
                "Delivery complete",
                f":white_check_mark: Accessioned `{accession_id}`. No unregistered files remain.",
            )
        else:
            session_id = str(uuid.uuid4())
            SESSIONS[session_id] = {
                "files": [str(path) for path in remaining],
                "user_id": body["user"]["id"],
                "channel_id": proposal.get("channel_id", ""),
                "kind": "reference" if is_reference else "sequencing",
            }
            if is_reference:
                next_view = build_reference_accession_modal(
                    remaining, service.reference_vocabularies(), session_id,
                    proposal.get("channel_id", ""), body["user"]["id"],
                )
            else:
                next_view = build_accession_modal(
                    remaining, vocabularies=service.vocabularies(), session_id=session_id,
                    workflows=service.workflow_definitions(),
                    channel_id=proposal.get("channel_id", ""), user_id=body["user"]["id"],
                )
        client.views_update(
            view_id=body["view"]["id"], hash=body["view"].get("hash"), view=next_view
        )
    except Exception as exc:
        logger.exception("Accession-and-next failed")
        client.views_update(
            view_id=body["view"]["id"], hash=body["view"].get("hash"),
            view=_message_view("Accession failed", f":warning: {exc}"),
        )


def handle_accession_reject(ack: Ack, body: dict, client: WebClient):
    ack()
    proposal = PROPOSALS.pop(body["actions"][0]["value"], None)
    client.views_update(
        view_id=body["view"]["id"], hash=body["view"].get("hash"),
        view=_message_view("Accession rejected", "No registry changes were made."),
    )
    if proposal and proposal.get("channel_id"):
        client.chat_postMessage(
            channel=proposal["channel_id"],
            text=f"<@{body['user']['id']}> rejected the proposed accession. No changes were made.",
        )
