from slack_bolt import Ack, Respond

from instance import get_instance


def build_help_text() -> str:
    instance = get_instance()
    return (
        f"*{instance.name} commands*\n"
        f"• `{instance.command('status')}` — show new and missing files\n"
        f"• `{instance.command('sequencing-accession')}` — review and register sequencing files\n"
        f"• `{instance.command('reference-accession')}` — review and register files in the reference inbox\n"
        f"• `{instance.command('help')}` — show this help\n\n"
        "No sequencing or reference files are moved, renamed, modified, or deleted."
    )


def handle_help_command(ack: Ack, respond: Respond):
    ack()
    respond(response_type="ephemeral", text=build_help_text())
