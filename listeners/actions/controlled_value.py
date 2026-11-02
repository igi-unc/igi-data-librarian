from logging import Logger

from slack_bolt import Ack
from slack_sdk import WebClient


def handle_add_controlled_value(
    ack: Ack, body: dict, client: WebClient, logger: Logger
):
    ack()
    action = body["actions"][0]
    selected = action.get("selected_option") or {}
    if selected.get("value") != "Add a new value…":
        return
    try:
        view = body["view"]
        field = action["action_id"].removeprefix("controlled_select__")
        blocks = list(view["blocks"])
        for index, block in enumerate(blocks):
            if block.get("block_id") == field:
                blocks[index] = {
                    "type": "input",
                    "block_id": field,
                    "optional": block.get("optional", False),
                    "label": block["label"],
                    "hint": {
                        "type": "plain_text",
                        "text": "This value will become a dropdown option after approval.",
                    },
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Enter a new value"},
                        "focus_on_load": True,
                    },
                }
                break
        updated_view = {
            "type": "modal",
            "callback_id": view["callback_id"],
            "private_metadata": view.get("private_metadata", ""),
            "title": view["title"],
            "submit": view["submit"],
            "close": view["close"],
            "blocks": blocks,
        }
        client.views_update(
            view_id=view["id"], hash=view.get("hash"), view=updated_view
        )
    except Exception:
        logger.exception("Could not switch controlled field to free text")
