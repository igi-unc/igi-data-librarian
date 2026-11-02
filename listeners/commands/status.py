from logging import Logger

from slack_bolt import Ack, Respond

from instance import get_instance
from registry import RegistryService


def _sample(paths, limit=8):
    shown = [f"• `{path.name}`" for path in paths[:limit]]
    if len(paths) > limit:
        shown.append(f"• …and {len(paths) - limit} more")
    return "\n".join(shown) or "• None"


def handle_status_command(ack: Ack, respond: Respond, logger: Logger):
    ack()
    try:
        status = RegistryService.from_environment().status()
        title = f"{get_instance().label} registry status"
        respond(
            response_type="in_channel",
            text=title,
            blocks=[
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
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*New files*\n{_sample(status.unregistered)}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*New reference files*\n{_sample(status.unregistered_references)}"}},
            ],
        )
    except Exception as exc:
        logger.exception("Registry status failed")
        respond(response_type="ephemeral", text=f":warning: Status failed: {exc}")
