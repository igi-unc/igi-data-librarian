from logging import Logger

from slack_bolt import Ack
from slack_sdk import WebClient

from instance import get_instance
from listeners.views.raft_builder import build_raft_modal
from pipelines.jobs import runner


def handle_run_command(ack: Ack, body: dict, client: WebClient, logger: Logger):
    ack()
    instance = get_instance()

    def tell(text: str) -> None:
        client.chat_postEphemeral(channel=body["channel_id"], user=body["user_id"], text=text)

    try:
        if not instance.is_approver(body["user_id"]):
            tell("Only approvers can run RAFT workflows. Ask an approver to run this for you.")
            return
        workflows = instance.raft.enabled_workflows
        if not workflows:
            pending = ", ".join(w.name for w in instance.raft.workflows) or "none configured"
            tell(f"No RAFT workflows are enabled yet (configured: {pending}).")
            return
        raft = runner()
        datasets, seen = [], set()
        for workflow in workflows:
            for dataset in raft.eligible_datasets(workflow):
                if dataset["internal_id"] not in seen:
                    seen.add(dataset["internal_id"])
                    datasets.append(dataset)
        if not datasets:
            tell("No registered datasets have input files these workflows can use.")
            return
        client.views_open(trigger_id=body["trigger_id"],
                          view=build_raft_modal(list(workflows), datasets, body["channel_id"]))
    except Exception as exc:
        logger.exception("Could not open the RAFT form")
        tell(f":warning: Could not open the RAFT form: {exc}")
