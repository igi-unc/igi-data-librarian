"""Render a Slack app manifest for one librarian instance.

Usage:
    python -m instance.manifest instances/pirl.toml -o manifest.json
    python -m instance.manifest instances/igi.toml -o manifests/igi.json

The instance file defaults to $DATA_LIBRARIAN_INSTANCE when no path is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import InstanceConfig, get_instance, load_instance

COMMAND_DESCRIPTIONS = {
    "status": "Show new and missing {label} files",
    "sequencing-accession": "Review and accession sequencing files",
    "reference-accession": "Review and accession reference files",
    "help": "Show {name} commands",
}


def suggested_prompts(config: InstanceConfig) -> list[dict[str, str]]:
    return [
        {"title": "Registry status", "message": f"What is the current {config.label} registry status?"},
        {"title": "New files", "message": "Are there unregistered files?"},
        {"title": "Accession help", "message": "How do I accession a new dataset?"},
    ]


def build_manifest(config: InstanceConfig) -> dict:
    return {
        "display_information": {"name": config.name, "description": config.description},
        "features": {
            "agent_view": {
                "agent_description": (
                    f"Monitor sequencing files, explain registry status, and prepare reviewed {config.label} accessions."
                ),
                "suggested_prompts": suggested_prompts(config)[:2],
            },
            "app_home": {
                "home_tab_enabled": True,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {"display_name": config.bot_display_name, "always_online": True},
            "slash_commands": [
                {
                    "command": command,
                    "description": COMMAND_DESCRIPTIONS[key].format(label=config.label, name=config.name),
                    "should_escape": False,
                }
                for key, command in config.commands.items()
            ],
        },
        "oauth_config": {
            "redirect_urls": ["https://ngrok-free.app/slack/oauth_redirect"],
            "scopes": {
                "bot": [
                    "app_mentions:read",
                    "chat:write",
                    "commands",
                    "im:history",
                    "reactions:write",
                    "assistant:write",
                ]
            },
        },
        "settings": {
            "event_subscriptions": {
                "request_url": "https://ngrok-free.app/slack/events",
                "bot_events": ["app_home_opened", "app_mention", "message.im"],
            },
            "interactivity": {"is_enabled": True, "request_url": "https://ngrok-free.app/slack/events"},
            "is_mcp_enabled": False,
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("instance", nargs="?", help="Instance TOML file (default: $DATA_LIBRARIAN_INSTANCE)")
    parser.add_argument("-o", "--output", help="Write to this file instead of stdout")
    args = parser.parse_args(argv)

    config = load_instance(args.instance) if args.instance else get_instance()
    text = json.dumps(build_manifest(config), indent=2) + "\n"
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
