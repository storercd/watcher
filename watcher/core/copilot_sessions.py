"""
List local GitHub Copilot app sessions, most recently active first, for the session picker.

Only metadata is read: directory names, file mtimes, ``workspace.yaml`` and the
``inuse.<pid>.lock`` markers. Event logs are not parsed here.
"""

from __future__ import annotations

import glob
import os
import re
from typing import Any, Dict, List

from watcher.watchers import copilot_session

DEFAULT_LIMIT = 5
_UUID_RE = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")


def _pid_alive(pid: int) -> bool:
    """
    Whether a process with this pid exists.

    Returns:
        True if the pid is running (even if owned by another user).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _is_open(session_dir: str) -> bool:
    """
    Whether a live Copilot process holds the session open.

    Returns:
        True if any ``inuse.<pid>.lock`` names a running pid.
    """
    for lock in glob.glob(os.path.join(session_dir, "inuse.*.lock")):
        try:
            with open(lock, encoding="utf-8") as handle:
                pid = int(handle.read().strip())
        except (OSError, ValueError):
            continue
        if pid > 0 and _pid_alive(pid):
            return True
    return False


def _read_meta(session_dir: str) -> Dict[str, str]:
    """
    Read the simple ``key: value`` lines of workspace.yaml.

    Returns:
        The values by key; empty if the file is unreadable.
    """
    meta: Dict[str, str] = {}
    try:
        with open(os.path.join(session_dir, "workspace.yaml"), encoding="utf-8") as handle:
            for line in handle:
                key, sep, value = line.partition(":")
                if sep and not line[:1].isspace():
                    meta[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    return meta


def _status(path: str) -> Dict[str, str]:
    """
    Summarize the session's state from the tail of its event log.

    Returns:
        ``status`` and ``detail`` (as the session watcher would report them); empty strings if unreadable.
    """
    try:
        status, detail, _marker = copilot_session.derive_status(
            copilot_session._read_events(os.path.join(path, "events.jsonl"))
        )
    except OSError:
        return {"status": "", "detail": ""}
    return {"status": status.value, "detail": detail}


def list_sessions(limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    """
    Non-archived Copilot sessions: open ones first, then the rest, each newest activity first.

    "Open" means a live process holds the session's lock file. Recency is the mtime of
    ``events.jsonl`` (``workspace.yaml``'s ``updated_at`` can lag behind).
    Directories without a ``workspace.yaml`` are skipped.

    Returns:
        Dicts with id, title, repository, branch, cwd, updated (epoch seconds), open, status and detail.
    """
    root = copilot_session.SESSION_STATE_DIR
    try:
        names = os.listdir(root)
    except OSError:
        return []
    candidates = []
    for name in names:
        if not _UUID_RE.match(name):
            continue
        path = os.path.join(root, name)
        try:
            os.stat(os.path.join(path, "workspace.yaml"))
            try:
                mtime = os.stat(os.path.join(path, "events.jsonl")).st_mtime
            except OSError:
                mtime = os.stat(os.path.join(path, "workspace.yaml")).st_mtime
        except OSError:
            continue
        candidates.append((_is_open(path), mtime, name, path))
    candidates.sort(reverse=True)

    sessions: List[Dict[str, Any]] = []
    for is_open, mtime, name, path in candidates:
        if len(sessions) >= limit:
            break
        if copilot_session.is_archived(name):
            continue
        meta = _read_meta(path)
        sessions.append(
            {
                "id": name,
                "title": copilot_session.app_title(name) or meta.get("name", ""),
                "repository": meta.get("repository", ""),
                "branch": meta.get("branch", ""),
                "cwd": meta.get("cwd", ""),
                "updated": mtime,
                "open": is_open,
                **_status(path),
            }
        )
    return sessions
