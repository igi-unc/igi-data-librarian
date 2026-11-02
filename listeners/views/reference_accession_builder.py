import json

from registry.service import REFERENCE_CONTROLLED_VALUES


TEXT_FIELDS = [
    ("name", "Reference name", False, "e.g. GRCh38 or GENCODE"),
    ("assembly", "Assembly", True, "e.g. GRCh38, hg38, GRCm39"),
    ("version", "Version", True, "e.g. release 48"),
    ("provider", "Provider", True, "e.g. GENCODE, Ensembl, NCBI"),
    ("source_url", "Source URL", True, "original download or documentation URL"),
    ("lbg_path", "LBG path override", True, "defaults to selected reference files"),
    ("longleaf_path", "Longleaf path", True, "optional"),
    ("primary_contact_person", "Primary contact", True, "optional"),
    ("notes", "Notes", True, "usage, provenance, or compatibility notes"),
]


def _option(value):
    return {"text": {"type": "plain_text", "text": value}, "value": value}


def build_reference_accession_modal(files, vocabularies, session_id, channel_id, user_id):
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"Select the files belonging to one logical reference from these *{len(files)} unregistered reference files*."}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "A reference may contain several related files, such as a FASTA, GTF, and prebuilt index."}]},
        {"type": "divider"},
    ]
    for start in range(0, len(files), 10):
        chunk = files[start:start + 10]
        blocks.append({
            "type": "input", "block_id": f"selected_reference_files_{start // 10}", "optional": True,
            "label": {"type": "plain_text", "text": "Reference files" if len(files) <= 10 else f"Files {start + 1}–{start + len(chunk)}"},
            "element": {"type": "checkboxes", "action_id": "selection", "options": [
                {"text": {"type": "plain_text", "text": path.name[:75]}, "value": str(index)}
                for index, path in enumerate(chunk, start=start)
            ]},
        })
    for field in REFERENCE_CONTROLLED_VALUES:
        options = [_option(value) for value in vocabularies.get(field, [])] + [_option("Add a new value…")]
        blocks.append({
            "type": "input", "block_id": f"reference.{field}", "optional": field != "reference_type", "dispatch_action": True,
            "label": {"type": "plain_text", "text": field.replace("_", " ").title()},
            "hint": {"type": "plain_text", "text": {
                "reference_type": "The role this reference serves, such as genome, annotation, index, panel, or database.",
                "species": "The organism represented; leave blank for organism-independent resources.",
                "file_format": "The primary format of the selected files.",
            }[field]},
            "element": {"type": "static_select", "action_id": f"controlled_select__reference.{field}", "options": options,
                        "placeholder": {"type": "plain_text", "text": f"Select {field.replace('_', ' ')}"}},
        })
    for field, label, optional, placeholder in TEXT_FIELDS:
        blocks.append({
            "type": "input", "block_id": f"reference.{field}", "optional": optional,
            "label": {"type": "plain_text", "text": label},
            "element": {"type": "plain_text_input", "action_id": "value", "placeholder": {"type": "plain_text", "text": placeholder}},
        })
    return {
        "type": "modal", "callback_id": "reference_accession_submit",
        "private_metadata": json.dumps({"session_id": session_id, "channel_id": channel_id, "user_id": user_id}),
        "title": {"type": "plain_text", "text": "Accession reference"},
        "submit": {"type": "plain_text", "text": "Review"},
        "close": {"type": "plain_text", "text": "Cancel"}, "blocks": blocks,
    }
