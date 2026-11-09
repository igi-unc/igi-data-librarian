"""Where messages go.

With ``[privacy] protect_phi = true`` the bot follows one rule: details that may contain
PHI (sample IDs, dataset IDs, LIMS details) go only to the user who asked, in a modal or a
direct message. Channels get file names, counts and channel-safe labels (LIMS number,
participant ID, RAFT project ID).
"""

from __future__ import annotations

from logging import Logger

from slack_sdk import WebClient


def is_direct_message(channel_id: str | None) -> bool:
    return bool(channel_id) and channel_id.startswith("D")


def dm(client: WebClient, user_id: str, text: str, blocks: list | None = None) -> dict:
    """Send a direct message from the bot to one user (the app's Messages tab)."""
    kwargs = {"channel": user_id, "text": text}
    if blocks:
        kwargs["blocks"] = blocks
    return client.chat_postMessage(**kwargs)


def post_to_channel(client: WebClient, channel_id: str | None, text: str, logger: Logger | None = None) -> None:
    """Post a channel-safe message; skipped when the request came from a DM or no channel."""
    if not channel_id or is_direct_message(channel_id):
        return
    try:
        client.chat_postMessage(channel=channel_id, text=text)
    except Exception:
        if logger:
            logger.exception("Could not post to channel %s", channel_id)
