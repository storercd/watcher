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
    return {"type": kind, "data": data, "id": f"id-{kind}-{len(json.dumps(data))}"}


class CopilotSessionWatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(copilot_session, "SESSION_STATE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.makedirs(os.path.join(self.tmp.name, SID))
        self.w = CopilotSessionWatcher(SID)

    def write(self, events, extra=""):
        with open(os.path.join(self.tmp.name, SID, "events.jsonl"), "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
            f.write(extra)

    def test_matches(self):
        self.assertTrue(CopilotSessionWatcher.matches_url(SID))
        self.assertTrue(CopilotSessionWatcher.matches_url("copilot-session:" + SID))
        self.assertTrue(CopilotSessionWatcher.matches_url("ghapp://sessions/" + SID))
        self.assertFalse(CopilotSessionWatcher.matches_url("https://example.com"))

    def test_running_then_done_notifies_once(self):
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
        base = [ev("assistant.turn_start"), ev("tool.execution_start", toolCallId="a", toolName="ask_user",
                                                arguments={"question": "Ok?"})]
        self.write(base)
        r = self.w.check()
        self.assertEqual(r.status, Status.APPROVAL_REQUIRED)
        self.assertIn("Ok?", r.detail)
        self.write(base + [ev("tool.execution_complete", toolCallId="a")])
        self.assertEqual(self.w.check().status, Status.BUILDING)

    def test_permission_pending(self):
        self.write([ev("permission.requested", requestId="r", permissionRequest={"toolName": "bash"})])
        self.assertEqual(self.w.check().status, Status.APPROVAL_REQUIRED)
        self.write([ev("permission.requested", requestId="r"), ev("permission.completed", requestId="r")])
        self.assertNotEqual(self.w.check().status, Status.APPROVAL_REQUIRED)

    def test_abort_and_bad_lines(self):
        self.write([ev("assistant.turn_start"), ev("abort", reason="user_initiated")], extra='{"type": "x"')
        self.assertEqual(self.w.check().status, Status.IDLE)

    def test_missing_log_is_error(self):
        self.assertEqual(self.w.check().status, Status.ERROR)

    def test_config_roundtrip(self):
        again = CopilotSessionWatcher.from_config(self.w.to_config())
        self.assertEqual(again.session_id, SID)
        self.assertEqual(again.display_url, "ghapp://sessions/" + SID)


if __name__ == "__main__":
    unittest.main()
