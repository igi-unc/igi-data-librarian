import json


def _option(text: str, value: str) -> dict:
    return {"text": {"type": "plain_text", "text": text[:75]}, "value": value}


def dataset_option_label(dataset: dict) -> str:
    row = dataset["row"]
    bits = [row.get("lims_number") and f"LIMS #{row['lims_number']}", row.get("source"),
            row.get("sample_type"), row.get("analyte"), row.get("library_type")]
    label = " · ".join(bit for bit in bits if bit)
    return f"{label} ({dataset['created_at'][:10]})" if label else dataset["dataset_id"]


def build_raft_modal(workflows, datasets, channel_id: str) -> dict:
    """Pick a workflow and one registered dataset (the modal is visible only to the user)."""
    return {
        "type": "modal", "callback_id": "raft_run_submit",
        "private_metadata": json.dumps({"channel_id": channel_id}),
        "title": {"type": "plain_text", "text": "Run a RAFT workflow"},
        "submit": {"type": "plain_text", "text": "Set up"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                "The bot links the dataset's files into its RAFT workspace and runs RAFT setup. "
                "Nothing runs on the cluster until you press *Launch* in the message it sends you."
            )}},
            {"type": "input", "block_id": "workflow", "label": {"type": "plain_text", "text": "Workflow"},
             "element": {"type": "static_select", "action_id": "value",
                         "options": [_option(f"{w.name} ({w.workflow} {w.version})", w.id) for w in workflows],
                         **({"initial_option": _option(f"{workflows[0].name} ({workflows[0].workflow} {workflows[0].version})", workflows[0].id)} if len(workflows) == 1 else {})}},
            {"type": "input", "block_id": "dataset", "label": {"type": "plain_text", "text": "Dataset"},
             "element": {"type": "static_select", "action_id": "value",
                         "options": [_option(dataset_option_label(d), d["internal_id"]) for d in datasets[:100]]}},
        ],
    }


def build_ready_blocks(run_id: str, project_id: str, workflow, staged_files: int, dataset_id: str) -> list[dict]:
    """DM shown when setup finished: summary plus Launch / Cancel."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f":gear: RAFT setup finished for project `{project_id}`.\n"
            f"*Workflow:* {workflow.name} (`{workflow.workflow}` {workflow.version}, {workflow.species}, {workflow.input_data})\n"
            f"*Dataset:* `{dataset_id}`\n*Linked input files:* {staged_files}"
        )}},
        {"type": "actions", "block_id": "raft_ready", "elements": [
            {"type": "button", "action_id": "raft_launch", "value": run_id, "style": "primary",
             "text": {"type": "plain_text", "text": "Launch"},
             "confirm": {"title": {"type": "plain_text", "text": "Launch RAFT run?"},
                         "text": {"type": "plain_text", "text": f"Submit {project_id} to the cluster."},
                         "confirm": {"type": "plain_text", "text": "Launch"},
                         "deny": {"type": "plain_text", "text": "Not yet"}}},
            {"type": "button", "action_id": "raft_cancel", "value": run_id, "style": "danger",
             "text": {"type": "plain_text", "text": "Cancel"}},
        ]},
    ]
