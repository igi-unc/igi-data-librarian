from instance import get_instance


def build_app_home_view(install_url: str | None = None, is_connected: bool = False) -> dict:
    del install_url, is_connected
    instance = get_instance()
    return {
        "type": "home",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": instance.name}},
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                f"Monitor and accession {instance.label} sequencing and reference files.\n\n"
                f"• `{instance.command('status')}` — list new and missing files\n"
                f"• `{instance.command('sequencing-accession')}` — prepare a reviewed sequencing accession\n"
                f"• `{instance.command('reference-accession')}` — prepare a reviewed reference accession\n"
                f"• `{instance.command('help')}` — show command help"
                + (f"\n• `{instance.command('run')}` — set up and launch a RAFT workflow (approvers)"
                   if instance.command_enabled("run") else "")
            )}},
            {"type": "divider"},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": (
                "Registry writes require explicit approval. Sequencing files are never moved, modified, or deleted."
            )}]},
        ],
    }
