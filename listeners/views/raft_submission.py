import json
from logging import Logger

from slack_bolt import Ack
from slack_sdk import WebClient

from instance import get_instance
from listeners.notify import dm, post_to_channel
from listeners.views.raft_builder import build_ready_blocks
from pipelines import RaftError
from pipelines.jobs import EXECUTOR, runner


def prepare_and_offer(client: WebClient, run_id: str, user_id: str, channel_id: str, logger: Logger) -> None:
    """Background: run RAFT setup, then DM the user a Launch button."""
    try:
        result = runner().prepare(run_id)
    except Exception as exc:
        logger.exception("RAFT setup failed for %s", run_id)
        dm(client, user_id, f":x: RAFT setup failed: {exc}")
        return
    blocks = build_ready_blocks(run_id, result["project_id"], result["workflow"], result["staged_files"],
                                result["dataset"]["dataset_id"])
    dm(client, user_id, f"RAFT setup finished for {result['project_id']}", blocks)


def handle_raft_submission(ack: Ack, body: dict, client: WebClient, logger: Logger):
    user_id = body["user"]["id"]
    instance = get_instance()
    if not instance.is_approver(user_id):
        ack(response_action="errors", errors={"workflow": "Only approvers can run RAFT workflows."})
        return
    state = body["view"]["state"]["values"]
    workflow_key = state["workflow"]["value"]["selected_option"]["value"]
    dataset_id = state["dataset"]["value"]["selected_option"]["value"]
    channel_id = json.loads(body["view"].get("private_metadata") or "{}").get("channel_id", "")
    raft = runner()
    workflow = instance.raft.workflow(workflow_key)
    if dataset_id not in {d["internal_id"] for d in raft.eligible_datasets(workflow)}:
        ack(response_action="errors", errors={"dataset": f"{workflow.name} can't run on this dataset."})
        return
    try:
        run_id, project_id = raft.request(workflow_key, dataset_id, user_id, channel_id)
    except RaftError as exc:
        ack(response_action="errors", errors={"dataset": str(exc)})
        return
    ack(response_action="clear")
    dm(client, user_id, f":hourglass_flowing_sand: Setting up RAFT {workflow.name} project `{project_id}`. "
                        "I'll message you here when it's ready to launch.")
    post_to_channel(client, channel_id,
                    f"<@{user_id}> is setting up RAFT {workflow.name} project `{project_id}`.", logger)
    EXECUTOR.submit(prepare_and_offer, client, run_id, user_id, channel_id, logger)
