"""Tests for the Copilot session picker listing."""

from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from watcher.core import copilot_sessions
from watcher.watchers import copilot_session

IDS = [f"{i:08x}-0000-0000-0000-000000000000" for i in range(1, 5)]


def _make(root, sid, mtime, pid=None, title="t"):
    d = root / sid
    d.mkdir()
    (d / "workspace.yaml").write_text(f"id: {sid}\nname: {title}\nrepository: o/repo\nbranch: b\ncwd: /w\n")
    events = d / "events.jsonl"
    events.write_text(json.dumps({"type": "assistant.turn_start", "id": "1", "data": {}}) + "\n")
    os.utime(events, (mtime, mtime))
    if pid is not None:
        (d / f"inuse.{pid}.lock").write_text(str(pid))


@pytest.fixture
def root(tmp_path):
    """Point the session state dir at a temp dir with no app DB and a fake pid check."""
    with mock.patch.object(copilot_session, "SESSION_STATE_DIR", str(tmp_path)), mock.patch.object(
        copilot_session, "APP_DB_PATH", str(tmp_path / "none.db")
    ), mock.patch.object(copilot_sessions, "_pid_alive", lambda pid: pid != 999):
        yield tmp_path


def test_open_first_then_recent(root):
    """Open sessions sort ahead of the rest, each by recency."""
    _make(root, IDS[0], 100, pid=1)
    _make(root, IDS[1], 300, pid=1, title="new")
    _make(root, IDS[2], 200)  # no lock
    _make(root, IDS[3], 400, pid=999)  # stale lock
    result = copilot_sessions.list_sessions(5)
    assert [s["id"] for s in result] == [IDS[1], IDS[0], IDS[3], IDS[2]]
    assert [s["open"] for s in result] == [True, True, False, False]
    assert result[0]["title"] == "new"
    assert result[0]["status"] == "building"


def test_limit(root):
    """The limit caps the result."""
    for i, sid in enumerate(IDS[:3]):
        _make(root, sid, 100 + i, pid=1)
    assert len(copilot_sessions.list_sessions(2)) == 2


def test_archived_skipped(root):
    """Archived sessions are not listed."""
    _make(root, IDS[0], 100, pid=1)
    with mock.patch.object(copilot_session, "is_archived", lambda sid: True):
        assert copilot_sessions.list_sessions(5) == []
