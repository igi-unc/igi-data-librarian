"""Background work that must not run inside a Slack request (Slack expects a reply in 3 s)."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from slack_sdk import WebClient

from instance import get_instance
from registry import RegistryService

from .raft import RaftRunner

logger = logging.getLogger(__name__)

# RAFT setup is slow (it fetches workflow modules); two at a time is plenty.
EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="raft-setup")


def runner() -> RaftRunner:
    return RaftRunner(get_instance(), RegistryService.from_environment())


def report_updates(client: WebClient) -> None:
    """Poll running RAFT runs once and tell people about any that finished."""
    from listeners.notify import dm, post_to_channel  # imported here: listeners imports this module

    for update in runner().poll():
        icon = ":white_check_mark:" if update.status == "completed" else ":x:"
        text = (f"{icon} RAFT {update.workflow_name} project `{update.project_id}` {update.status} "
                f"({update.detail}).")
        try:
            dm(client, update.requested_by, text)
        except Exception:
            logger.exception("Could not DM RAFT update for %s", update.project_id)
        post_to_channel(client, update.channel_id, text, logger)


def start_monitor(client: WebClient) -> threading.Thread | None:
    """Start the background RAFT monitor when this instance has a [raft] section."""
    config = get_instance().raft
    if config is None:
        return None
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(config.poll_seconds):
            try:
                report_updates(client)
            except Exception:
                logger.exception("RAFT monitor pass failed")

    thread = threading.Thread(target=loop, name="raft-monitor", daemon=True)
    thread.stop = stop  # type: ignore[attr-defined]
    thread.start()
    return thread
