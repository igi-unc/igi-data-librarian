from slack_bolt import App
import re

from .feedback_buttons import handle_feedback_button
from .controlled_value import handle_add_controlled_value
from .accession import (
    handle_accession_approve,
    handle_accession_approve_next,
    handle_accession_reject,
)


def register(app: App):
    app.action(re.compile(r"^controlled_select__"))(handle_add_controlled_value)
    app.action("feedback")(handle_feedback_button)
    app.action("accession_approve")(handle_accession_approve)
    app.action("accession_approve_next")(handle_accession_approve_next)
    app.action("accession_reject")(handle_accession_reject)
