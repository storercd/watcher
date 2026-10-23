"""Unit tests for the Copilot app session watcher, using synthetic event logs."""

import json
import os
import tempfile
import unittest
from unittest import mock

from watcher.core.base import Status
from watcher.watchers import copilot_session
from watcher.watchers.copilot_session import CopilotSessionWatcher

SID = "11111111-2222-3333-4444-555555555555"


def ev(kind, **data):
    """Ev."""
    return {"type": kind, "data": data, "id": f"id-{kind}-{len(json.dumps(data))}"}


class CopilotSessionWatcherTests(unittest.TestCase):
    """Status derivation and config round-tripping."""

    def setUp(self):
        """Setup."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(copilot_session, "SESSION_STATE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.makedirs(os.path.join(self.tmp.name, SID))
        self.w = CopilotSessionWatcher(SID)

    def write(self, events, extra=""):
        """Write."""
        with open(os.path.join(self.tmp.name, SID, "events.jsonl"), "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
            f.write(extra)

    def test_matches(self):
        """Test matches."""
        self.assertTrue(CopilotSessionWatcher.matches_url(SID))
        self.assertTrue(CopilotSessionWatcher.matches_url("copilot-session:" + SID))
        self.assertTrue(CopilotSessionWatcher.matches_url("ghapp://sessions/" + SID))
        self.assertFalse(CopilotSessionWatcher.matches_url("https://example.com"))

    def test_running_then_done_notifies_once(self):
        """Test running then done notifies once."""
        self.write([ev("session.start"), ev("user.message"), ev("assistant.turn_start", turnId="0")])
        self.assertEqual(self.w.check().status, Status.BUILDING)
        self.write([ev("session.start"), ev("assistant.turn_start"), ev("assistant.turn_end", turnId="0")])
        r = self.w.check()
        self.assertEqual(r.status, Status.SUCCESS)
        self.assertTrue(r.newly_actionable)
        self.assertFalse(self.w.check().newly_actionable)
        self.w.acknowledge()
        self.assertFalse(self.w.check().unacknowledged)

    def test_ask_user_pending_then_answered(self):
        """Test ask user pending then answered."""
        base = [ev("assistant.turn_start"), ev("tool.execution_start", toolCallId="a", toolName="ask_user",
                                                arguments={"question": "Ok?"})]
        self.write(base)
        r = self.w.check()
        self.assertEqual(r.status, Status.APPROVAL_REQUIRED)
        self.assertIn("Ok?", r.detail)
        self.write(base + [ev("tool.execution_complete", toolCallId="a")])
        self.assertEqual(self.w.check().status, Status.BUILDING)

    def test_permission_pending(self):
        """Test permission pending."""
        self.write([ev("permission.requested", requestId="r", permissionRequest={"toolName": "bash"})])
        self.assertEqual(self.w.check().status, Status.APPROVAL_REQUIRED)
        self.write([ev("permission.requested", requestId="r"), ev("permission.completed", requestId="r")])
        self.assertNotEqual(self.w.check().status, Status.APPROVAL_REQUIRED)

    def test_abort_and_bad_lines(self):
        """Test abort and bad lines."""
        self.write([ev("assistant.turn_start"), ev("abort", reason="user_initiated")], extra='{"type": "x"')
        self.assertEqual(self.w.check().status, Status.IDLE)

    def test_missing_log_is_error(self):
        """Test missing log is error."""
        self.assertEqual(self.w.check().status, Status.ERROR)

    def test_config_roundtrip(self):
        """Test config roundtrip."""
        again = CopilotSessionWatcher.from_config(self.w.to_config())
        self.assertEqual(again.session_id, SID)
        self.assertEqual(again.display_url, "ghapp://sessions/" + SID)


if __name__ == "__main__":
    unittest.main()


def test_is_archived(tmp_path):
    import sqlite3

    from watcher.watchers.copilot_session import is_archived

    db = str(tmp_path / "data.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE workspaces (id TEXT, session_id TEXT, archived_at TEXT)")
    conn.execute("INSERT INTO workspaces VALUES ('w1', 's1', '2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO workspaces VALUES ('w2', 's2', NULL)")
    conn.commit()
    conn.close()
    assert is_archived("s1", db)
    assert is_archived("w1", db)
    assert not is_archived("s2", db)
    assert not is_archived("s1", str(tmp_path / "missing.db"))
