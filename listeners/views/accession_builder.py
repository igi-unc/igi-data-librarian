from datetime import datetime
import json
import re

from instance import get_instance
from registry.service import CONTROLLED_VALUES


TEXT_FIELDS = [
    ("sequenced_year", "Sequenced year", True, "YYYY"),
    ("sample_id", "Sample ID", False, "optional"),
    ("perturbation", "Perturbation", False, "optional"),
    ("bio_rep_id", "Biological replicate", False, "optional"),
    ("tech_rep_id", "Technical replicate", False, "optional"),
    ("sequencing_provider", "Sequencing provider", False, "optional"),
    ("lbg_path", "LBG path override", False, "defaults to selected sequencing files"),
    ("longleaf_path", "Longleaf path", False, "optional"),
    ("labarchives_path", "LabArchives path", False, "optional"),
    ("sample_type_origin", "Sample origin", False, "e.g. brain, lymphoma"),
    ("capture_kit", "Capture kit", False, "optional"),
    ("library_primary_contact_person", "Primary contact", False, "optional"),
    ("notes", "Notes", False, "optional"),
]

REQUIRED_SELECTS = {"source", "sample_type", "analyte", "platform", "library_type", "species"}


def _label(field: str) -> str:
    return field.replace("_", " ").title()


def _option(value: str) -> dict:
    return {"text": {"type": "plain_text", "text": value}, "value": value}


def _inferred(files) -> dict[str, str]:
    names = " ".join(path.name for path in files)
    result = {}
    if re.search(r"(?:^|[_\.])R1(?:[_\.]|$)", names, re.I) and re.search(
        r"(?:^|[_\.])R2(?:[_\.]|$)", names, re.I
    ):
        result["sequencing_read_type"] = "paired-end"
    return result


def build_accession_modal(
    files, vocabularies=None, workflows=None, session_id: str = "", channel_id: str = "", user_id: str = ""
) -> dict:
    vocabularies = vocabularies or CONTROLLED_VALUES
    inferred = _inferred(files)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"Select the files belonging to one logical dataset from these *{len(files)} unregistered files*."}},
        {"type": "divider"},
    ]
    for chunk_start in range(0, len(files), 10):
        chunk = files[chunk_start : chunk_start + 10]
        chunk_number = chunk_start // 10
        blocks.append({
            "type": "input", "block_id": f"selected_files_{chunk_number}", "optional": True,
            "label": {"type": "plain_text", "text": (
                "Files to accession" if len(files) <= 10
                else f"Files {chunk_start + 1}–{chunk_start + len(chunk)}"
            )},
            "element": {
                "type": "checkboxes", "action_id": "selection",
                "options": [
                    {"text": {"type": "plain_text", "text": path.name[:75]}, "value": str(index)}
                    for index, path in enumerate(chunk, start=chunk_start)
                ],
            },
        })
    for field in CONTROLLED_VALUES:
        values = vocabularies.get(field, [])
        options = [_option(value) for value in values] + [_option("Add a new value…")]
        element = {
            "type": "static_select", "action_id": f"controlled_select__{field}", "options": options,
            "placeholder": {"type": "plain_text", "text": f"Select {_label(field).lower()}"},
        }
        if inferred.get(field) in values:
            element["initial_option"] = _option(inferred[field])
        blocks.append({
            "type": "input", "block_id": field, "optional": field not in REQUIRED_SELECTS,
            "dispatch_action": True,
            "label": {"type": "plain_text", "text": _label(field)}, "element": element,
        })
    for field, label, required, placeholder in TEXT_FIELDS:
        element = {"type": "plain_text_input", "action_id": "value", "placeholder": {"type": "plain_text", "text": placeholder}}
        if field == "sequenced_year":
            element["initial_value"] = str(datetime.now().year)
        blocks.append({"type": "input", "block_id": field, "optional": not required,
                       "label": {"type": "plain_text", "text": label}, "element": element})
    blocks.extend([
        {"type": "divider"},
        {"type": "input", "block_id": "known_hla_alleles", "optional": True,
         "label": {"type": "plain_text", "text": "Known HLA alleles"},
         "hint": {"type": "plain_text", "text": "Optional existing calls. Separate alleles with spaces, commas, or new lines; for example HLA-A*02:01, A*03:01, B*07:02."},
         "element": {"type": "plain_text_input", "action_id": "value", "multiline": True,
                     "placeholder": {"type": "plain_text", "text": "HLA-A*02:01\nHLA-A*03:01\nHLA-B*07:02"}}},
        {"type": "input", "block_id": "known_hla_source", "optional": True,
         "label": {"type": "plain_text", "text": "Known HLA source or method"},
         "hint": {"type": "plain_text", "text": "Optional provenance, such as clinical typing, OptiType, arcasHLA, or a report identifier."},
         "element": {"type": "plain_text_input", "action_id": "value",
                     "placeholder": {"type": "plain_text", "text": "Clinical typing report or caller/version"}}},
    ])
    if workflows:
        blocks.extend([
            {"type": "input", "block_id": "requested_workflows", "optional": True,
             "label": {"type": "plain_text", "text": "Analyses to request"},
             "hint": {"type": "plain_text", "text": "Requests are recorded durably. RAFT execution remains pending until its entrypoints are configured."},
             "element": {"type": "checkboxes", "action_id": "selection", "options": [
                 {"text": {"type": "plain_text", "text": workflow["name"][:75]},
                  "description": {"type": "plain_text", "text": workflow["description"][:75]},
                  "value": workflow["workflow_id"]}
                 for workflow in workflows
             ]}},
        ])
    return {
        "type": "modal", "callback_id": "accession_submit",
        "private_metadata": json.dumps({"session_id": session_id, "channel_id": channel_id, "user_id": user_id}),
        "title": {"type": "plain_text", "text": f"Accession {get_instance().label} files"[:24]},
        "submit": {"type": "plain_text", "text": "Review"},
        "close": {"type": "plain_text", "text": "Cancel"}, "blocks": blocks,
    }


def build_review_view(
    proposal_id: str, row: dict, files: list[str], warnings: list[str], workflows: list[dict] | None = None,
    provided_hla_calls: list[dict] | None = None,
) -> dict:
    metadata = "\n".join(f"*{key}:* `{value}`" for key, value in row.items() if value and key != "notes")
    warning_text = "\n".join(f"• :warning: {item}" for item in warnings)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Proposed accession*\n{metadata}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Files:* {len(files)}\n*Notes:* {row['notes'] or 'None'}"}},
    ]
    if workflows:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": (
            "*Requested analyses:*\n" + "\n".join(f"• {workflow['name']}" for workflow in workflows)
        )}})
    if provided_hla_calls:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": (
            "*Known HLA alleles supplied:*\n" + "\n".join(f"• `{call['allele']}`" for call in provided_hla_calls)
        )}})
    if warning_text:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": warning_text}})
    blocks.append({"type": "actions", "elements": [
        {"type": "button", "action_id": "accession_approve", "value": proposal_id, "style": "primary", "text": {"type": "plain_text", "text": "Approve and commit"}},
        {"type": "button", "action_id": "accession_approve_next", "value": proposal_id, "text": {"type": "plain_text", "text": "Approve and accession next"}},
        {"type": "button", "action_id": "accession_reject", "value": proposal_id, "style": "danger", "text": {"type": "plain_text", "text": "Reject"}},
    ]})
    return {"type": "modal", "callback_id": "accession_review", "title": {"type": "plain_text", "text": "Review accession"}, "close": {"type": "plain_text", "text": "Close"}, "blocks": blocks}
