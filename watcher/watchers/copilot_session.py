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

Archived sessions (per the app's ``data.db``) keep their status, with " - archived"
appended to the detail, like merged PRs.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from watcher.core.base import CheckResult, Status, Watcher
from watcher.core.registry import register

SESSION_STATE_DIR = os.path.join(os.path.expanduser("~"), ".copilot", "session-state")
APP_DB_PATH = os.path.join(os.path.expanduser("~"), ".copilot", "data.db")
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
        The parsed events in file order; unreadable files raise OSError.
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


class _SessionState:
    """Mutable fold state over a session's events."""

    def __init__(self) -> None:
        self.pending_tools: Dict[str, str] = {}
        self.pending_perms: Dict[str, str] = {}
        self.turn_open = False
        self.last_end: Optional[str] = None
        self.aborted = False
        self.closed = False
        self.question = ""

    def reset(self, _event: Dict[str, Any], _data: Dict[str, Any]) -> None:
        """Start of a (resumed) session run: drop everything in flight."""
        self.pending_tools.clear()
        self.pending_perms.clear()
        self.turn_open = self.aborted = self.closed = False

    def turn_start(self, _event: Dict[str, Any], _data: Dict[str, Any]) -> None:
        """A user message or new turn means the agent is working."""
        self.turn_open, self.aborted = True, False

    def turn_end(self, event: Dict[str, Any], data: Dict[str, Any]) -> None:
        """The agent finished a turn."""
        self.turn_open = False
        self.last_end = str(event.get("id") or data.get("turnId") or "")

    def tool_start(self, _event: Dict[str, Any], data: Dict[str, Any]) -> None:
        """Track a tool call; ask_user calls stay pending until answered."""
        self.pending_tools[str(data.get("toolCallId"))] = str(data.get("toolName"))
        if data.get("toolName") == "ask_user":
            self.question = str((data.get("arguments") or {}).get("question") or "")

    def tool_end(self, _event: Dict[str, Any], data: Dict[str, Any]) -> None:
        """A tool call completed."""
        self.pending_tools.pop(str(data.get("toolCallId")), None)

    def perm_start(self, _event: Dict[str, Any], data: Dict[str, Any]) -> None:
        """A permission prompt is waiting on the user."""
        req = data.get("permissionRequest") or {}
        self.pending_perms[str(data.get("requestId"))] = str(req.get("toolName") or req.get("kind") or "")

    def perm_end(self, _event: Dict[str, Any], data: Dict[str, Any]) -> None:
        """A permission prompt was answered."""
        self.pending_perms.pop(str(data.get("requestId")), None)

    def abort(self, _event: Dict[str, Any], _data: Dict[str, Any]) -> None:
        """The user interrupted the agent."""
        self.pending_tools.clear()
        self.pending_perms.clear()
        self.turn_open, self.aborted = False, True

    def shutdown(self, _event: Dict[str, Any], _data: Dict[str, Any]) -> None:
        """The session process closed."""
        self.closed = True

    def result(self) -> Tuple[Status, str, str]:
        """
        Summarize the folded state.

        Returns:
            A tuple of (status, detail, marker).
        """
        asks = [cid for cid, name in self.pending_tools.items() if name == "ask_user"]
        if asks:
            return Status.APPROVAL_REQUIRED, f"Needs your input: {self.question}".strip(), f"ask:{asks[-1]}"
        if self.pending_perms:
            req_id, name = next(reversed(self.pending_perms.items()))
            return Status.APPROVAL_REQUIRED, f"Waiting for permission: {name}".strip(), f"perm:{req_id}"
        if self.closed and not self.last_end:
            return Status.IDLE, "Session closed", ""
        if self.turn_open or self.pending_tools:
            return Status.BUILDING, "Running", ""
        if self.aborted:
            return Status.IDLE, "Stopped by user", ""
        if self.last_end:
            return Status.SUCCESS, "Done - waiting for you", f"turn:{self.last_end}"
        return Status.IDLE, "No activity yet", ""


def derive_status(events: List[Dict[str, Any]]) -> Tuple[Status, str, str]:
    """
    Fold an event list into (status, detail, marker).

    The marker identifies the thing the user should look at (the finished
    turn or the pending prompt) so each is notified and acknowledged once.

    Returns:
        A tuple of (status, detail, marker).
    """
    state = _SessionState()
    handlers = {
        "session.start": state.reset,
        "session.resume": state.reset,
        "user.message": state.turn_start,
        "assistant.turn_start": state.turn_start,
        "assistant.turn_end": state.turn_end,
        "tool.execution_start": state.tool_start,
        "tool.execution_complete": state.tool_end,
        "permission.requested": state.perm_start,
        "permission.completed": state.perm_end,
        "abort": state.abort,
        "session.shutdown": state.shutdown,
    }
    for event in events:
        handler = handlers.get(event.get("type"))
        if handler is None:
            continue
        data = event.get("data")
        handler(event, data if isinstance(data, dict) else {})
    return state.result()


def is_archived(session_id: str, db_path: str = APP_DB_PATH) -> bool:
    """
    Whether the Copilot app has archived the session.

    The app records this in its ``workspaces`` table (``archived_at``). Any
    failure to read it (missing or locked DB, schema change) means "not archived".

    Returns:
        True if the session's workspace has an archive timestamp.
    """
    if not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
        try:
            row = conn.execute(
                "SELECT 1 FROM workspaces WHERE (session_id = ? OR id = ?) AND archived_at IS NOT NULL LIMIT 1",
                (session_id, session_id),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return row is not None


def agent_merge_pr(session_id: str, db_path: str = APP_DB_PATH) -> str:
    """
    The open PR the Copilot app's agent merge is watching for this session.

    Any failure to read the DB (missing or locked, schema change) means "not watching".

    Returns:
        A label like ``PR #42`` when agent merge is enabled and the session's PR is
        open and unmerged, otherwise an empty string.
    """
    if not os.path.exists(db_path):
        return ""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
        try:
            row = conn.execute(
                "SELECT created_pr_number FROM workspaces WHERE (session_id = ? OR id = ?) "
                "AND agent_merge_enabled = 1 AND created_pr_number IS NOT NULL "
                "AND lower(COALESCE(created_pr_state, '')) = 'open' AND created_pr_merged_at IS NULL LIMIT 1",
                (session_id, session_id),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return ""
    return f"PR #{row[0]}" if row else ""


def app_title(session_id: str, db_path: str = APP_DB_PATH) -> str:
    """
    The session title shown in the Copilot app.

    The app stores it in the ``workspaces`` table (``name``); sessions started from a
    GitHub issue keep the placeholder ``New session`` in ``workspace.yaml``. Any
    failure to read it means "no title".

    Returns:
        The title, or an empty string if unavailable.
    """
    if not os.path.exists(db_path):
        return ""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
        try:
            row = conn.execute(
                "SELECT name FROM workspaces WHERE (session_id = ? OR id = ?) "
                "AND name IS NOT NULL AND name != '' LIMIT 1",
                (session_id, session_id),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return ""
    return str(row[0]).strip() if row else ""


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
        Use the app's session title, else the name from workspace.yaml.

        Returns:
            The derived label.
        """
        title = app_title(self.session_id)
        if title:
            return f"Copilot: {title}"
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

        if marker.startswith("turn:"):
            pr = agent_merge_pr(self.session_id)
            if pr:
                status, detail, marker = Status.IDLE, f"Watching {pr} (agent merge)", ""

        if is_archived(self.session_id):
            detail = f"{detail} - archived"

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
