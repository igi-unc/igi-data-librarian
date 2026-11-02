from slack_bolt import App

from .accession_submission import handle_accession_submission
from .reference_accession_submission import handle_reference_accession_submission


def register(app: App):
    app.view("accession_submit")(handle_accession_submission)
    app.view("reference_accession_submit")(handle_reference_accession_submission)
