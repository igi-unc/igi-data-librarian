from logging import Logger

from slack_bolt import Ack, Respond
from slack_sdk import WebClient

from instance import get_instance
from listeners.notify import dm, is_direct_message
from registry import RegistryService


def _sample(paths, limit=8):
    shown = [f"• `{path.name}`" for path in paths[:limit]]
    if len(paths) > limit:
        shown.append(f"• …and {len(paths) - limit} more")
    return "\n".join(shown) or "• None"


def _file_sections(status) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*New files*\n{_sample(status.unregistered)}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*New reference files*\n{_sample(status.unregistered_references)}"}},
    ]


def build_status_blocks(status, title: str, *, include_files: bool) -> list[dict]:
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": title}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Datasets*\n{status.datasets}"},
            {"type": "mrkdwn", "text": f"*Sequencing files monitored*\n{status.files}"},
            {"type": "mrkdwn", "text": f"*Unregistered*\n{len(status.unregistered)}"},
            {"type": "mrkdwn", "text": f"*Missing*\n{len(status.missing)}"},
            {"type": "mrkdwn", "text": f"*References*\n{status.reference_datasets}"},
            {"type": "mrkdwn", "text": f"*New reference files*\n{len(status.unregistered_references)}"},
            {"type": "mrkdwn", "text": f"*Requested workflows*\n{status.requested_workflows}"},
            {"type": "mrkdwn", "text": f"*Running workflows*\n{status.running_workflows}"},
            {"type": "mrkdwn", "text": f"*Failed workflows*\n{status.failed_workflows}"},
        ]},
    ]
    if include_files:
        blocks.extend(_file_sections(status))
    return blocks


def handle_status_command(ack: Ack, respond: Respond, body: dict, client: WebClient, logger: Logger):
    ack()
    try:
        instance = get_instance()
        status = RegistryService.from_environment().status()
        title = f"{instance.label} registry status"
        private = is_direct_message(body.get("channel_id"))
        if not instance.protect_phi or private:
            respond(response_type="in_channel", text=title,
                    blocks=build_status_blocks(status, title, include_files=True))
            return
        # File names can carry sample IDs: counts in the channel, names only to the requester.
        blocks = build_status_blocks(status, title, include_files=False)
        has_files = status.unregistered or status.unregistered_references
        if has_files:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": (
                f"File names were sent to <@{body['user_id']}> in a direct message."
            )}]})
        respond(response_type="in_channel", text=title, blocks=blocks)
        if has_files:
            dm(client, body["user_id"], f"{title}: new file names", _file_sections(status))
    except Exception as exc:
        logger.exception("Registry status failed")
        respond(response_type="ephemeral", text=f":warning: Status failed: {exc}")
