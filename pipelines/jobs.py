"""Background work that must not run inside a Slack request (Slack expects a reply in 3 s)."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from slack_sdk import WebClient

from instance import get_instance
from registry import RegistryService

from .batches import BatchUpdate, PatientBatcher, Queued
from .raft import RaftRunner

logger = logging.getLogger(__name__)

# How often the monitor wakes to check per-patient timers.
TICK_SECONDS = 60

# RAFT setup is slow (it fetches workflow modules); two at a time is plenty.
EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="raft-setup")


def runner() -> RaftRunner:
    return RaftRunner(get_instance(), RegistryService.from_environment())


def _when(moment) -> str:
    """Slack renders this in each reader's own time zone."""
    stamp = int(moment.timestamp())
    return f"<!date^{stamp}^{{time}}|{moment.strftime('%H:%M UTC')}>"


def describe_queued(item: Queued) -> str:
    """One channel-safe line about a dataset joining a per-patient automatic run."""
    name = f"{item.workflow_name} for {item.patient} (`{item.project_id}`)"
    if item.project_running and item.started_timer:
        return (f":hourglass_flowing_sand: {name}: a run is in progress; this sample goes in the next run, "
                f"no earlier than {_when(item.due_at)}.")
    if item.started_timer:
        return (f":hourglass_flowing_sand: {name} starts at {_when(item.due_at)}. "
                f"Other {item.patient} samples accessioned before then join the same run.")
    return f":heavy_plus_sign: Added to {name}, which starts at {_when(item.due_at)}."


def queue_automatic_runs(dataset_internal_id: str, requested_by: str, channel_id: str) -> list[str]:
    """Queue a newly accessioned dataset for automatic workflows; returns lines to post."""
    if get_instance().raft is None or not get_instance().raft.auto_workflows:
        return []
    return [describe_queued(item) for item in PatientBatcher(runner()).enqueue(
        dataset_internal_id, requested_by, channel_id)]


def describe_batch(update: BatchUpdate) -> str:
    name = f"{update.workflow_name} for {update.patient} (`{update.project_id}`)"
    if update.status == "started":
        return (f":rocket: {name} started: {update.samples} sample(s), {update.new_samples} new "
                f"({update.detail}). Finished samples are reused from the cache.")
    icon = ":white_check_mark:" if update.status == "completed" else ":x:"
    return f"{icon} {name} {update.status} ({update.detail})."


def report_batches(client: WebClient, *, poll: bool) -> None:
    """Start per-patient runs whose timers ended and, when asked, report finished ones."""
    from listeners.notify import dm, is_direct_message, post_to_channel

    batcher = PatientBatcher(runner())
    updates = batcher.dispatch_due()
    if poll:
        updates += batcher.poll()
    for update in updates:
        text = describe_batch(update)
        in_channel = bool(update.channel_id) and not is_direct_message(update.channel_id)
        post_to_channel(client, update.channel_id, text, logger)
        if update.status != "started" or not in_channel:
            for user_id in update.requested_by:
                try:
                    dm(client, user_id, text)
                except Exception:
                    logger.exception("Could not DM RAFT update for %s", update.project_id)


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
        last_poll = time.monotonic()
        while not stop.wait(min(TICK_SECONDS, config.poll_seconds)):
            poll = time.monotonic() - last_poll >= config.poll_seconds
            try:
                report_batches(client, poll=poll)
                if poll:
                    report_updates(client)
            except Exception:
                logger.exception("RAFT monitor pass failed")
            if poll:
                last_poll = time.monotonic()

    thread = threading.Thread(target=loop, name="raft-monitor", daemon=True)
    thread.stop = stop  # type: ignore[attr-defined]
    thread.start()
    return thread
