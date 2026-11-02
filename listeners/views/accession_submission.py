import json
import uuid
from logging import Logger

from slack_bolt import Ack

from listeners.views.accession_builder import build_review_view
from listeners.accession_state import PROPOSALS, SESSIONS
from instance import get_instance
from registry import RegistryService
from registry.service import CONTROLLED_VALUES


def handle_accession_submission(ack: Ack, body: dict, logger: Logger):
    try:
        state = body["view"]["state"]["values"]
        values = {}
        errors = {}
        selected_indexes = []
        selected_workflow_ids = []
        known_hla_text = ""
        known_hla_source = ""
        for field, actions in state.items():
            action = next(iter(actions.values()))
            if field.startswith("selected_files_"):
                selected_indexes.extend(
                    int(option["value"]) for option in action.get("selected_options", [])
                )
                continue
            if field == "requested_workflows":
                selected_workflow_ids = [option["value"] for option in action.get("selected_options", [])]
                continue
            if field == "known_hla_alleles":
                known_hla_text = action.get("value") or ""
                continue
            if field == "known_hla_source":
                known_hla_source = (action.get("value") or "").strip()
                continue
            if field in CONTROLLED_VALUES:
                if action.get("type") == "plain_text_input":
                    values[field] = (action.get("value") or "").strip()
                else:
                    selected = action.get("selected_option")
                    values[field] = selected.get("value", "") if selected else ""
            else:
                values[field] = action.get("value") or ""
        if errors:
            ack(response_action="errors", errors=errors)
            return
        if not values["sequenced_year"].isdigit() or len(values["sequenced_year"]) != 4:
            ack(response_action="errors", errors={"sequenced_year": "Enter a four-digit year."})
            return
        metadata = json.loads(body["view"].get("private_metadata") or "{}")
        session = SESSIONS.get(metadata.get("session_id"))
        if not session or session["user_id"] != body["user"]["id"]:
            raise ValueError(f"This accession form expired; run {get_instance().command('sequencing-accession')} again.")
        selected_files = [session["files"][index] for index in selected_indexes]
        row, paths, warnings = RegistryService.from_environment().propose(
            values, selected_files=selected_files
        )
        service = RegistryService.from_environment()
        workflows = service.validate_workflows(selected_workflow_ids, row)
        try:
            provided_hla_calls = service.parse_provided_hla(known_hla_text)
        except ValueError as exc:
            ack(response_action="errors", errors={"known_hla_alleles": str(exc)})
            return
        for call in provided_hla_calls:
            call["metadata_json"] = {"source_or_method": known_hla_source} if known_hla_source else {}
        proposal_id = str(uuid.uuid4())
        PROPOSALS[proposal_id] = {
            "row": row, "files": [str(path) for path in paths],
            "user_id": body["user"]["id"],
            "channel_id": metadata.get("channel_id", ""),
            "workflow_ids": selected_workflow_ids,
            "workflows": workflows,
            "provided_hla_calls": provided_hla_calls,
        }
        ack(response_action="update", view=build_review_view(
            proposal_id, row, PROPOSALS[proposal_id]["files"], warnings, workflows, provided_hla_calls
        ))
    except Exception as exc:
        logger.exception("Accession proposal failed")
        ack(response_action="errors", errors={"source": f"Could not prepare accession: {exc}"})
