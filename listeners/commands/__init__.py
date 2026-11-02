from slack_bolt import App

from instance import get_instance

from .accession import handle_accession_command
from .reference_accession import handle_reference_accession_command
from .help import handle_help_command
from .status import handle_status_command


def register(app: App):
    instance = get_instance()
    app.command(instance.command("status"))(handle_status_command)
    app.command(instance.command("sequencing-accession"))(handle_accession_command)
    app.command(instance.command("reference-accession"))(handle_reference_accession_command)
    app.command(instance.command("help"))(handle_help_command)
