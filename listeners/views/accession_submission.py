import json
import uuid
from logging import Logger

from slack_bolt import Ack
from slack_sdk import WebClient

from instance import get_instance
from lims import LimsError, lims_client
from listeners.accession_state import PROPOSALS, SESSIONS
from listeners.views.accession_builder import (
    LIMS_FIELD_MAP,
    REQUIRED_SELECTS,
    build_message_view,
    build_review_view,
)
from registry import RegistryService
from registry.service import CONTROLLED_VALUES


def _parse_state(state: dict) -> dict:
    values, selected_indexes, workflow_ids = {}, [], []
    known_hla_text = known_hla_source = ""
    write_back = False
    for field, actions in state.items():
        action = next(iter(actions.values()))
        if field.startswith("selected_files_"):
            selected_indexes.extend(int(option["value"]) for option in action.get("selected_options", []))
        elif field == "requested_workflows":
            workflow_ids = [option["value"] for option in action.get("selected_options", [])]
        elif field == "lims_write_back":
            write_back = any(option["value"] == "write_back" for option in action.get("selected_options", []))
        elif field == "known_hla_alleles":
            known_hla_text = action.get("value") or ""
        elif field == "known_hla_source":
            known_hla_source = (action.get("value") or "").strip()
        elif field in CONTROLLED_VALUES:
            if action.get("type") == "plain_text_input":
                values[field] = (action.get("value") or "").strip()
            else:
                selected = action.get("selected_option")
                values[field] = selected.get("value", "") if selected else ""
        else:
            values[field] = action.get("value") or ""
    return {"values": values, "selected_indexes": selected_indexes, "workflow_ids": workflow_ids,
            "known_hla_text": known_hla_text, "known_hla_source": known_hla_source, "write_back": write_back}


def apply_lims_fields(values: dict, safe: dict, vocabularies: dict) -> list[str]:
    """Fill blank fields from the LIMS's channel-safe fields; return warnings for conflicts."""
    warnings = []
    for lims_field, field in LIMS_FIELD_MAP.items():
        lims_value = (safe.get(lims_field) or "").strip()
        if not lims_value:
            continue
        current = (values.get(field) or "").strip()
        allowed = vocabularies.get(field)
        if not current:
            if field != "source" and allowed and lims_value not in allowed:
                warnings.append(f"The LIMS {field.replace('_', ' ')} `{lims_value}` is not a registry option; fill it in yourself.")
                continue
            values[field] = lims_value
        elif current != lims_value:
            warnings.append(f"{field.replace('_', ' ').capitalize()}: you chose `{current}`, the LIMS says `{lims_value}`.")
    return warnings


def handle_accession_submission(ack: Ack, body: dict, client: WebClient, logger: Logger):
    acked = False
    view_id = body["view"]["id"]
    try:
        instance = get_instance()
        parsed = _parse_state(body["view"]["state"]["values"])
        values = parsed["values"]
        if not values.get("sequenced_year", "").isdigit() or len(values["sequenced_year"]) != 4:
            ack(response_action="errors", errors={"sequenced_year": "Enter a four-digit year."})
            return
        metadata = json.loads(body["view"].get("private_metadata") or "{}")
        session = SESSIONS.get(metadata.get("session_id"))
        if not session or session["user_id"] != body["user"]["id"]:
            raise ValueError(f"This accession form expired; run {instance.command('sequencing-accession')} again.")

        service = RegistryService.from_environment()
        try:
            provided_hla_calls = service.parse_provided_hla(parsed["known_hla_text"])
        except ValueError as exc:
            ack(response_action="errors", errors={"known_hla_alleles": str(exc)})
            return

        client_lims = lims_client(instance.lims)
        sample_key = (values.get("sample_id") or "").strip()
        use_lims = bool(client_lims and sample_key)
        if not use_lims:
            missing = {field: "Required." for field in REQUIRED_SELECTS if not values.get(field)}
            if missing:
                if client_lims:
                    missing = {field: "Required, or enter a sample ID to fill it from the LIMS." for field in missing}
                ack(response_action="errors", errors=missing)
                return

        lims_summary, lims_warnings, tube = None, [], None
        if use_lims:
            # The LIMS can take a few seconds; Slack needs an answer within 3.
            ack(response_action="update", view=build_message_view("Checking the LIMS", ":hourglass_flowing_sand: Checking this tube with the LIMS…"))
            acked = True
            try:
                tube = client_lims.registry_fields(sample_key)
            except ValueError as exc:
                client.views_update(view_id=view_id, view=build_message_view("Accession stopped", f":warning: {exc}"))
                return
            except LimsError as exc:
                lims_warnings.append(f"LIMS check skipped: {exc}")
                lims_summary = "not checked"
            else:
                if not tube.found:
                    client.views_update(view_id=view_id, view=build_message_view(
                        "Accession stopped",
                        ":warning: No tube with that sample ID or LIMS number is in the LIMS. Nothing was registered.",
                    ))
                    return
                lims_warnings += apply_lims_fields(values, tube.safe, service.vocabularies())
                values["lims_number"] = tube.lims_number
                lims_summary = f"tube found ({tube.label})"
            missing = [field for field in sorted(REQUIRED_SELECTS) if not values.get(field)]
            if missing:
                client.views_update(view_id=view_id, view=build_message_view("Accession stopped", (
                    ":warning: These fields are still blank after the LIMS check: "
                    + ", ".join(f"`{field}`" for field in missing)
                    + f". Run {instance.command('sequencing-accession')} again and fill them in."
                )))
                return

        selected_files = [session["files"][index] for index in parsed["selected_indexes"]]
        row, paths, warnings = service.propose(values, selected_files=selected_files)
        workflows = service.validate_workflows(parsed["workflow_ids"], row)
        for call in provided_hla_calls:
            call["metadata_json"] = {"source_or_method": parsed["known_hla_source"]} if parsed["known_hla_source"] else {}

        write_back = bool(
            parsed["write_back"] and tube and tube.found and instance.is_approver(body["user"]["id"])
        )
        if lims_summary and tube and tube.found:
            lims_summary += "; will record the dataset on this tube" if write_back else "; will not write to the LIMS"
        proposal_id = str(uuid.uuid4())
        PROPOSALS[proposal_id] = {
            "row": row, "files": [str(path) for path in paths],
            "user_id": body["user"]["id"],
            "channel_id": metadata.get("channel_id", ""),
            "workflow_ids": parsed["workflow_ids"],
            "workflows": workflows,
            "provided_hla_calls": provided_hla_calls,
            "lims_write_back": write_back,
            "lims_label": tube.label if tube and tube.found else "",
        }
        review = build_review_view(
            proposal_id, row, PROPOSALS[proposal_id]["files"], lims_warnings + warnings, workflows,
            provided_hla_calls, lims_summary,
        )
        if acked:
            client.views_update(view_id=view_id, view=review)
        else:
            ack(response_action="update", view=review)
    except Exception as exc:
        logger.exception("Accession proposal failed")
        if acked:
            client.views_update(view_id=view_id, view=build_message_view("Accession failed", f":warning: Could not prepare accession: {exc}"))
        else:
            ack(response_action="errors", errors={"source": f"Could not prepare accession: {exc}"})
