"""
GitHub Copilot app session watcher plugin.

Accepts a session ID (a bare UUID, ``copilot-session:<uuid>`` or ``ghapp://sessions/<uuid>``) and derives
the session's state from the append-only event log at
``~/.copilot/session-state/<id>/events.jsonl``. This log format is
undocumented, so parsing is tolerant: unparsable lines and unknown event
types are ignored.

* pending ``ask_user`` tool call or permission request -> needs input
* open turn or pending tool call -> running
* last turn finished -> done (waiting on the user)
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from watcher.core.base import CheckResult, Status, Watcher
from watcher.core.registry import register

SESSION_STATE_DIR = os.path.join(os.path.expanduser("~"), ".copilot", "session-state")
URL_PREFIX = "ghapp://sessions/"
MAX_TAIL_BYTES = 4 * 1024 * 1024

_ID_RE = re.compile(r"^\s*(?:copilot-session:|ghapp://sessions/|github-app://sessions/)?([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})\s*$")


def _parse_session_id(text: str) -> str:
    """
    Extract the session UUID from user input.

    Returns:
        The lower-cased session ID.

    Raises:
        ValueError: If the text is not a session ID.
    """
    match = _ID_RE.match(text or "")
    if not match:
        raise ValueError(f"Not a recognized Copilot session ID: {text!r}")
    return match.group(1).lower()


def _read_events(path: str) -> List[Dict[str, Any]]:
    """
    Read the (tail of the) event log, skipping lines that don't parse.

    Returns:
        The parsed events in file order.

    Raises:
        OSError: If the file can't be read.
    """
    with open(path, "rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        truncated = size > MAX_TAIL_BYTES
        handle.seek(max(0, size - MAX_TAIL_BYTES))
        raw = handle.read()
    lines = raw.split(b"\n")
    if truncated:
        lines = lines[1:]  # first line is likely cut mid-record
    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def derive_status(events: List[Dict[str, Any]]) -> Tuple[Status, str, str]:
    """
    Fold an event list into (status, detail, marker).

    The marker identifies the thing the user should look at (the finished
    turn or the pending prompt) so each is notified and acknowledged once.
    """
    pending_tools: Dict[str, str] = {}
    pending_perms: Dict[str, str] = {}
    turn_open = False
    last_end: Optional[str] = None
    aborted = False
    closed = False
    question = ""

    for event in events:
        kind = event.get("type")
        data = event.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        if kind in ("session.start", "session.resume"):
            pending_tools.clear()
            pending_perms.clear()
            turn_open = aborted = closed = False
        elif kind == "user.message":
            turn_open, aborted = True, False
        elif kind == "assistant.turn_start":
            turn_open, aborted = True, False
        elif kind == "assistant.turn_end":
            turn_open = False
            last_end = str(event.get("id") or data.get("turnId") or "")
        elif kind == "tool.execution_start":
            call_id = str(data.get("toolCallId"))
            pending_tools[call_id] = str(data.get("toolName"))
            if data.get("toolName") == "ask_user":
                question = str((data.get("arguments") or {}).get("question") or "")
        elif kind == "tool.execution_complete":
            pending_tools.pop(str(data.get("toolCallId")), None)
        elif kind == "permission.requested":
            req = data.get("permissionRequest") or {}
            pending_perms[str(data.get("requestId"))] = str(req.get("toolName") or req.get("kind") or "")
        elif kind == "permission.completed":
            pending_perms.pop(str(data.get("requestId")), None)
        elif kind == "abort":
            pending_tools.clear()
            pending_perms.clear()
            turn_open, aborted = False, True
        elif kind == "session.shutdown":
            closed = True

    asks = [cid for cid, name in pending_tools.items() if name == "ask_user"]
    if asks:
        return Status.APPROVAL_REQUIRED, f"Needs your input: {question}".strip(), f"ask:{asks[-1]}"
    if pending_perms:
        req_id, name = next(reversed(pending_perms.items()))
        return Status.APPROVAL_REQUIRED, f"Waiting for permission: {name}".strip(), f"perm:{req_id}"
    if closed and not last_end:
        return Status.IDLE, "Session closed", ""
    if turn_open or pending_tools:
        return Status.BUILDING, "Running", ""
    if aborted:
        return Status.IDLE, "Stopped by user", ""
    if last_end:
        return Status.SUCCESS, "Done - waiting for you", f"turn:{last_end}"
    return Status.IDLE, "No activity yet", ""


@register
class CopilotSessionWatcher(Watcher):
    """Watches a local GitHub Copilot app session via its events.jsonl."""

    watcher_type = "copilot_session"

    def __init__(
        self,
        session_id: str,
        watcher_id: Optional[str] = None,
        label: str = "",
        last_completed: Optional[str] = None,
        acknowledged_completed: Optional[str] = None,
        notes: str = "",
    ):
        """Initialize a watcher for the session ID (or ``copilot-session:`` URL) given."""
        self.session_id = _parse_session_id(session_id)
        super().__init__(watcher_id=watcher_id, label=label, notes=notes)
        self.last_completed = last_completed
        self.acknowledged_completed = acknowledged_completed
        self._checked_since_start = False

    @property
    def session_dir(self) -> str:
        """Directory holding this session's state."""
        return os.path.join(SESSION_STATE_DIR, self.session_id)

    def default_label(self) -> str:
        """
        Use the session name from workspace.yaml if present.

        Returns:
            The derived label.
        """
        try:
            with open(os.path.join(self.session_dir, "workspace.yaml"), encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("name:"):
                        name = line.split(":", 1)[1].strip().strip("'\"")
                        if name:
                            return f"Copilot: {name}"
        except OSError:
            pass
        return f"Copilot session {self.session_id[:8]}"

    @property
    def display_url(self) -> Optional[str]:
        """Deep link that opens this session in the Copilot app."""
        return f"{URL_PREFIX}{self.session_id}"

    @classmethod
    def matches_url(cls, url: str) -> bool:
        """
        Whether the text is a session ID.

        Returns:
            True for a bare UUID or ``copilot-session:<uuid>``.
        """
        return bool(_ID_RE.match(url or ""))

    def check(self) -> CheckResult:
        """
        Read the event log and report the session's status.

        Returns:
            The result of inspecting the session.
        """
        path = os.path.join(self.session_dir, "events.jsonl")
        try:
            status, detail, marker = derive_status(_read_events(path))
        except OSError as exc:
            detail = f"Cannot read session log: {exc.strerror or exc}"
            self.last_status, self.last_detail = Status.ERROR, detail
            return CheckResult(status=Status.ERROR, detail=detail)

        unacknowledged = status.is_actionable and marker != self.acknowledged_completed
        newly_actionable = unacknowledged and (marker != self.last_completed or not self._checked_since_start)
        self._checked_since_start = True
        if status.is_actionable:
            self.last_completed = marker

        self.last_status = status
        self.last_detail = detail
        self.unacknowledged = unacknowledged
        return CheckResult(
            status=status, detail=detail, newly_actionable=newly_actionable, unacknowledged=unacknowledged
        )

    def acknowledge(self) -> None:
        """Mark the current turn/prompt as seen."""
        self.acknowledged_completed = self.last_completed
        self.unacknowledged = False

    def forget_acknowledgment(self) -> None:
        """Clear the acknowledged marker so the last state re-notifies."""
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
            "session_id": self.session_id,
            "last_completed": self.last_completed,
            "acknowledged_completed": self.acknowledged_completed,
            "notes": self.notes,
        }

    @classmethod
    def from_config(cls, data: Dict[str, Any]) -> "CopilotSessionWatcher":
        """
        Reconstruct from a dict produced by to_config().

        Returns:
            A new CopilotSessionWatcher instance.
        """
        return cls(
            session_id=data["session_id"],
            watcher_id=data.get("id"),
            label=data.get("label", ""),
            last_completed=data.get("last_completed"),
            acknowledged_completed=data.get("acknowledged_completed"),
            notes=data.get("notes", ""),
        )
