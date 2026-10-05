"""Background polling scheduler.

Runs each watcher's ``check()`` on its own timer thread and invokes a
callback on the main/GUI thread via a thread-safe queue so Tkinter (which is
not thread-safe) only ever gets touched from its own event loop.
"""

from __future__ import annotations

import queue
import threading
from typing import Dict, List

from watcher.core.base import CheckResult, Watcher


class Scheduler:
    """Polls a list of watchers on a background thread and queues results."""

    def __init__(self, poll_interval: int = 15):
        self.poll_interval = poll_interval
        self._watchers: Dict[str, Watcher] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread = None
        self.results: "queue.Queue[tuple[str, CheckResult]]" = queue.Queue()

    def set_watchers(self, watchers: List[Watcher]) -> None:
        self._watchers = {w.id: w for w in watchers}

    def add_watcher(self, watcher: Watcher) -> None:
        self._watchers[watcher.id] = watcher

    def remove_watcher(self, watcher_id: str) -> None:
        self._watchers.pop(watcher_id, None)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def poll_once_async(self) -> None:
        """Trigger an immediate out-of-band poll of all watchers (e.g. after add)."""
        threading.Thread(target=self._poll_all, daemon=True).start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._poll_all()
            self._stop_event.wait(self.poll_interval)

    def _poll_all(self) -> None:
        for watcher_id, watcher in list(self._watchers.items()):
            try:
                result = watcher.check()
            except Exception as exc:  # noqa: BLE001 - surface any plugin error
                from watcher.core.base import CheckResult, Status

                result = CheckResult(status=Status.ERROR, detail=str(exc))
            self.results.put((watcher_id, result))
