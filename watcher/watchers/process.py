"""
Running-process watcher.

Watches a local process (typically a command running in a terminal) and
notifies once it exits. Accepts ``proc://<pid>?started=<epoch>[&cmd=<text>]``.
The start time guards against pid reuse: if the pid is later taken by an
unrelated process, the watched one is considered finished.

The exit code of an unrelated process is not available, so a finished
process is always reported as success ("finished"), never failure.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, quote, unquote, urlsplit

from watcher.core import processes
from watcher.core.base import CheckResult, Status, Watcher
from watcher.core.registry import register

URL_PREFIX = "proc://"
# Start times come from ps with one-second resolution.
START_TOLERANCE_SECONDS = 2
MAX_LABEL_COMMAND = 60


def _parse_target(text: str) -> tuple:
    """
    Split ``proc://<pid>?started=<epoch>[&cmd=<text>]`` into its parts.

    Returns:
        A tuple of (pid, started_epoch, command).

    Raises:
        ValueError: If the text is not a valid process URL.
    """
    text = (text or "").strip()
    if not text.lower().startswith(URL_PREFIX):
        raise ValueError(f"Not a recognized process URL: {text!r}")
    parts = urlsplit(text)
    pid_text = unquote(parts.netloc or parts.path.lstrip("/"))
    if not pid_text.isdigit() or int(pid_text) <= 0:
        raise ValueError("Process URL needs a numeric pid")
    query = parse_qs(parts.query)
    try:
        started = float(query["started"][0])
    except (KeyError, ValueError) as exc:
        raise ValueError("Process URL needs a 'started' time") from exc
    return int(pid_text), started, query.get("cmd", [""])[0]


def build_url(pid: int, started: float, command: str = "") -> str:
    """
    Build the ``proc://`` URL for a process.

    Returns:
        The URL accepted by :class:`ProcessWatcher`.
    """
    url = f"{URL_PREFIX}{pid}?started={int(started)}"
    return f"{url}&cmd={quote(command, safe='')}" if command else url


def _format_elapsed(seconds: float) -> str:
    """
    Format a duration as ``4m`` / ``1h 5m`` / ``12s``.

    Returns:
        A short human-readable duration.
    """
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else f"{secs}s"


@register
class ProcessWatcher(Watcher):
    """Watches a local process for it to exit."""

    watcher_type = "process"

    def __init__(
        self,
        url: str,
        watcher_id: Optional[str] = None,
        label: str = "",
        last_completed: Optional[str] = None,
        acknowledged_completed: Optional[str] = None,
        notes: str = "",
    ):
        """Initialize from a ``proc://<pid>?started=<epoch>[&cmd=<text>]`` URL."""
        self.pid, self.started, self.command = _parse_target(url)
        super().__init__(watcher_id=watcher_id, label=label, notes=notes)
        self.last_completed = last_completed
        self.acknowledged_completed = acknowledged_completed
        self._notified_completed: Optional[str] = None

    def default_label(self) -> str:
        """
        Name the watcher after the command, or else the pid.

        Returns:
            The derived label.
        """
        command = self.command.strip()
        if len(command) > MAX_LABEL_COMMAND:
            command = command[: MAX_LABEL_COMMAND - 1] + "…"
        return f"Command: {command or f'pid {self.pid}'}"

    @property
    def display_url(self) -> Optional[str]:
        """The ``proc://`` URL, used for editing; it is not openable."""
        return build_url(self.pid, self.started, self.command)

    @classmethod
    def matches_url(cls, url: str) -> bool:
        """
        Whether the text is a ``proc://`` URL.

        Returns:
            True for valid ``proc://<pid>?started=<epoch>`` forms.
        """
        try:
            _parse_target(url)
        except ValueError:
            return False
        return True

    def _is_running(self) -> bool:
        started = processes.process_start_time(self.pid)
        return started is not None and abs(started - self.started) <= START_TOLERANCE_SECONDS

    def check(self) -> CheckResult:
        """
        Check whether the process is still running.

        Returns:
            ``building`` while it runs, then ``success`` (once, until
            acknowledged) after it exits.
        """
        if self._is_running():
            elapsed = _format_elapsed(time.time() - self.started)
            status, detail = Status.BUILDING, f"Running for {elapsed}"
        else:
            if self.last_completed is None:
                self.last_completed = str(time.time_ns())
            status, detail = Status.IDLE, "Process finished"

        unacknowledged = (
            self.last_completed is not None
            and self.last_completed != self.acknowledged_completed
            and status is Status.IDLE
        )
        if unacknowledged:
            status = Status.SUCCESS
        # Notify once per completion; a fresh process re-notifies a completion
        # that is still unacknowledged (like a restart with a pending result).
        newly_actionable = unacknowledged and self.last_completed != self._notified_completed
        if unacknowledged:
            self._notified_completed = self.last_completed
        self.last_status, self.last_detail, self.unacknowledged = status, detail, unacknowledged
        return CheckResult(
            status=status, detail=detail, newly_actionable=newly_actionable, unacknowledged=unacknowledged
        )

    def acknowledge(self) -> None:
        """Mark the completion as seen."""
        self.acknowledged_completed = self.last_completed
        self.unacknowledged = False

    def forget_acknowledgment(self) -> None:
        """Clear the acknowledged marker so the completion re-notifies."""
        self.acknowledged_completed = None

    def to_config(self) -> Dict[str, Any]:
        """
        Serialize for JSON config storage.

        Returns:
            A dict suitable for JSON config storage.
        """
        return {
            "watcher_type": self.watcher_type,
            "id": self.id,
            "label": self.label,
            "url": build_url(self.pid, self.started, self.command),
            "last_completed": self.last_completed,
            "acknowledged_completed": self.acknowledged_completed,
            "notes": self.notes,
        }

    @classmethod
    def from_config(cls, data: Dict[str, Any]) -> "ProcessWatcher":
        """
        Reconstruct from a dict produced by to_config().

        Returns:
            A new ProcessWatcher instance.
        """
        return cls(
            url=data["url"],
            watcher_id=data.get("id"),
            label=data.get("label", ""),
            last_completed=data.get("last_completed"),
            acknowledged_completed=data.get("acknowledged_completed"),
            notes=data.get("notes", ""),
        )
