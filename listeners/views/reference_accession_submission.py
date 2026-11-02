import json
import uuid
from logging import Logger

from slack_bolt import Ack

from listeners.accession_state import PROPOSALS, SESSIONS
from listeners.views.accession_builder import build_review_view
from instance import get_instance
from registry import RegistryService


def handle_reference_accession_submission(ack: Ack, body: dict, logger: Logger):
    try:
        state = body["view"]["state"]["values"]
        values, selected_indexes = {}, []
        for block_id, actions in state.items():
            action = next(iter(actions.values()))
            if block_id.startswith("selected_reference_files_"):
                selected_indexes.extend(int(option["value"]) for option in action.get("selected_options", []))
                continue
            field = block_id.removeprefix("reference.")
            if action.get("type") == "plain_text_input":
                values[field] = action.get("value") or ""
            else:
                selected = action.get("selected_option")
                values[field] = selected.get("value", "") if selected else ""
        metadata = json.loads(body["view"].get("private_metadata") or "{}")
        session = SESSIONS.get(metadata.get("session_id"))
        if not session or session["user_id"] != body["user"]["id"] or session.get("kind") != "reference":
            raise ValueError(f"This reference accession form expired; run {get_instance().command('reference-accession')} again.")
        selected_files = [session["files"][index] for index in selected_indexes]
        row, paths, warnings = RegistryService.from_environment().propose_reference(values, selected_files)
        proposal_id = str(uuid.uuid4())
        PROPOSALS[proposal_id] = {"row": row, "files": [str(path) for path in paths],
                                  "user_id": body["user"]["id"], "channel_id": metadata.get("channel_id", ""),
                                  "kind": "reference"}
        ack(response_action="update", view=build_review_view(proposal_id, row, PROPOSALS[proposal_id]["files"], warnings))
    except Exception as exc:
        logger.exception("Reference accession proposal failed")
        ack(response_action="errors", errors={"reference.name": f"Could not prepare accession: {exc}"})
