from logging import Logger

from slack_bolt import Ack
from slack_sdk import WebClient

from listeners.notify import post_to_channel
from pipelines.jobs import runner


def _replace_buttons(client: WebClient, body: dict, text: str) -> None:
    """Swap the Launch/Cancel buttons in the DM for an outcome line."""
    message = body.get("message") or {}
    blocks = [b for b in message.get("blocks", []) if b.get("type") != "actions"]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": text}]})
    client.chat_update(channel=body["channel"]["id"], ts=message["ts"], text=text, blocks=blocks)


def handle_raft_launch(ack: Ack, body: dict, client: WebClient, logger: Logger):
    ack()
    user_id = body["user"]["id"]
    run_id = body["actions"][0]["value"]
    try:
        raft = runner()
        job_id = raft.launch(run_id, approved_by=user_id)
        run = raft.registry.workflow_run(run_id)
        project_id = run["parameters"]["project_id"]
        text = f":rocket: <@{user_id}> launched RAFT {run['workflow_name']} project `{project_id}` ({job_id})."
        _replace_buttons(client, body, text)
        post_to_channel(client, run["parameters"].get("channel_id"), text, logger)
    except Exception as exc:
        logger.exception("RAFT launch failed")
        _replace_buttons(client, body, f":warning: Launch failed: {exc}")


def handle_raft_cancel(ack: Ack, body: dict, client: WebClient, logger: Logger):
    ack()
    try:
        runner().cancel(body["actions"][0]["value"], body["user"]["id"])
        _replace_buttons(client, body, f"Cancelled by <@{body['user']['id']}>. Nothing was submitted.")
    except Exception as exc:
        logger.exception("RAFT cancel failed")
        _replace_buttons(client, body, f":warning: Could not cancel: {exc}")
