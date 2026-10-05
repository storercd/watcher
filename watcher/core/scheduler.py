"""
Background polling scheduler.

Runs each watcher's ``check()`` on its own timer thread and invokes a
callback on the main/GUI thread via a thread-safe queue so Tkinter (which is
not thread-safe) only ever gets touched from its own event loop.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Dict, List

from watcher.core.base import CheckResult, Status, Watcher

logger = logging.getLogger("watcher.scheduler")


class Scheduler:
    """Polls a list of watchers on a background thread and queues results."""

    def __init__(self, poll_interval: int = 15):
        """Initialize the scheduler with a poll interval, in seconds, between rounds."""
        self.poll_interval = poll_interval
        self._watchers: Dict[str, Watcher] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread = None
        self.results: "queue.Queue[tuple[str, CheckResult]]" = queue.Queue()

    def set_watchers(self, watchers: List[Watcher]) -> None:
        """Replace the full set of watchers being polled."""
        self._watchers = {w.id: w for w in watchers}

    def add_watcher(self, watcher: Watcher) -> None:
        """Add a single watcher to the polled set."""
        self._watchers[watcher.id] = watcher

    def remove_watcher(self, watcher_id: str) -> None:
        """Remove a watcher from the polled set by id, if present."""
        self._watchers.pop(watcher_id, None)

    def start(self) -> None:
        """Start the background polling thread if it isn't already running."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="watcher-scheduler")
        self._thread.start()

    def stop(self) -> None:
        """Signal the background polling thread to stop."""
        self._stop_event.set()

    def poll_once_async(self) -> None:
        """Trigger an immediate out-of-band poll of all watchers (e.g. after add)."""
        logger.debug("spawning out-of-band poll_once_async thread")
        threading.Thread(target=self._poll_all, daemon=True, name="watcher-poll-once").start()

    def _run(self) -> None:
        logger.debug("scheduler thread started (poll_interval=%ss)", self.poll_interval)
        while not self._stop_event.is_set():
            self._poll_all()
            self._stop_event.wait(self.poll_interval)
        logger.debug("scheduler thread stopped")

    def _poll_all(self) -> None:
        watchers = list(self._watchers.items())
        logger.debug("poll round starting for %d watcher(s)", len(watchers))
        round_start = time.monotonic()
        for watcher_id, watcher in watchers:
            check_start = time.monotonic()
            try:
                result = watcher.check()
            except Exception as exc:  # noqa: BLE001 - surface any plugin error
                result = CheckResult(status=Status.ERROR, detail=str(exc))
            elapsed = time.monotonic() - check_start
            logger.debug(
                "checked %r (%s) in %.3fs -> %s",
                watcher.label, watcher_id, elapsed, result.status.value,
            )
            if elapsed > 2.0:
                logger.warning(
                    "watcher %r (%s) took %.1fs to check() - this runs on a "
                    "background thread but a slow/hung check delays every "
                    "later watcher in the same poll round",
                    watcher.label, watcher_id, elapsed,
                )
            self.results.put((watcher_id, result))
        logger.debug("poll round finished in %.3fs", time.monotonic() - round_start)
