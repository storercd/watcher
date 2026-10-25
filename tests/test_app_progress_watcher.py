"""Unit tests for the running-app progress watcher, with a faked Accessibility helper."""

import unittest
from unittest import mock

from watcher.core import ax_helper
from watcher.core.base import Status
from watcher.core.registry import detect_watcher_class
from watcher.watchers.app_progress import MISSING_POLLS_TO_FINISH_EARLY, AppProgressWatcher, build_url

URL = "app://org.python.python?window=Trail%20Camera"
BAR = {"running": True, "windows": 1, "progress": [{"value": 8, "min": 0, "max": 50}]}
NEAR_DONE_BAR = {"running": True, "windows": 1, "progress": [{"value": 49, "min": 0, "max": 50}]}
NO_BAR = {"running": True, "windows": 1, "progress": []}
NOT_RUNNING = {"running": False, "windows": 0, "progress": []}


class AppProgressWatcherTests(unittest.TestCase):
    """The "bar disappeared means ready" state machine."""

    def setUp(self):
        """Setup."""
        self.w = AppProgressWatcher(URL)
        patcher = mock.patch.object(ax_helper, "is_supported", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, reading):
        """Run one poll against a faked helper reading."""
        with mock.patch.object(ax_helper, "read_progress", return_value=reading):
            return self.w.check()

    def test_url_parsing_and_detection(self):
        """Bundle id and window title are parsed and the type is auto-detected."""
        self.assertEqual((self.w.bundle_id, self.w.window_title), ("org.python.python", "Trail Camera"))
        self.assertIs(detect_watcher_class(URL), AppProgressWatcher)
        self.assertIsNone(detect_watcher_class("https://example.com"))
        self.assertEqual(build_url("a.b", "x y"), "app://a.b?window=x%20y")

    def test_dock_option_round_trips_and_is_passed_to_helper(self):
        """``&dock=1`` is parsed, preserved in the URL, and forwarded to the helper."""
        w = AppProgressWatcher("app://com.adobe.LightroomClassicCC7?dock=1")
        self.assertTrue(w.dock)
        self.assertEqual(w.display_url, "app://com.adobe.LightroomClassicCC7?dock=1")
        self.assertEqual(build_url("a.b", "x y", dock=True), "app://a.b?window=x%20y&dock=1")
        self.assertFalse(self.w.dock)
        with mock.patch.object(ax_helper, "read_progress", return_value=BAR) as read:
            w.check()
        read.assert_called_once_with("com.adobe.LightroomClassicCC7", "", True)

    def test_idle_until_bar_seen(self):
        """No bar and never seen one: idle, no alert."""
        result = self.check(NO_BAR)
        self.assertEqual(result.status, Status.IDLE)
        self.assertFalse(result.newly_actionable)

    def test_early_disappearance_needs_many_polls(self):
        """A bar vanishing at 16% is treated as a glitch until it stays gone much longer."""
        self.check(BAR)
        for _ in range(MISSING_POLLS_TO_FINISH_EARLY - 1):
            self.assertEqual(self.check(NO_BAR).status, Status.BUILDING)
        self.assertEqual(self.check(NO_BAR).status, Status.SUCCESS)

    def test_building_then_ready_after_two_missing_polls(self):
        """Bar near completion is building; gone for two polls is success, notified once."""
        result = self.check(NEAR_DONE_BAR)
        self.assertEqual((result.status, result.detail), (Status.BUILDING, "Progress 98%"))
        result = self.check(NO_BAR)
        self.assertEqual(result.status, Status.BUILDING)
        self.assertFalse(result.newly_actionable)
        result = self.check(NO_BAR)
        self.assertEqual(result.status, Status.SUCCESS)
        self.assertTrue(result.newly_actionable)
        result = self.check(NO_BAR)
        self.assertEqual(result.status, Status.SUCCESS)
        self.assertFalse(result.newly_actionable)
        self.assertTrue(result.unacknowledged)

    def test_blip_does_not_finish(self):
        """A single missed poll followed by the bar returning is not completion."""
        self.check(BAR)
        self.check(NO_BAR)
        self.assertEqual(self.check(BAR).status, Status.BUILDING)
        self.assertEqual(self.check(NO_BAR).status, Status.BUILDING)

    def test_acknowledge_returns_to_idle(self):
        """Acknowledging a finished result clears it."""
        self.check(NEAR_DONE_BAR)
        self.check(NO_BAR)
        self.check(NO_BAR)
        self.w.acknowledge()
        result = self.check(NO_BAR)
        self.assertEqual(result.status, Status.IDLE)
        self.assertFalse(result.unacknowledged)

    def test_quit_is_not_done(self):
        """The app quitting mid-progress never alerts."""
        self.check(BAR)
        result = self.check(NOT_RUNNING)
        self.assertEqual(result.status, Status.IDLE)
        self.assertFalse(result.newly_actionable)
        self.assertEqual(self.check(NO_BAR).status, Status.IDLE)

    def test_config_round_trip_renotifies_after_restart(self):
        """An unacknowledged completion survives a restart and notifies again."""
        self.check(NEAR_DONE_BAR)
        self.check(NO_BAR)
        self.check(NO_BAR)
        restored = AppProgressWatcher.from_config(self.w.to_config())
        with mock.patch.object(ax_helper, "read_progress", return_value=NO_BAR):
            result = restored.check()
        self.assertEqual(result.status, Status.SUCCESS)
        self.assertTrue(result.newly_actionable)

    def test_helper_error_is_error_status(self):
        """Missing permission or helper surfaces as an error, not silence."""
        with mock.patch.object(ax_helper, "read_progress", side_effect=ax_helper.AxHelperError("denied")):
            result = self.w.check()
        self.assertEqual((result.status, result.detail), (Status.ERROR, "denied"))

    def test_unsupported_os_warns(self):
        """Non-macOS platforms report that there is no implementation."""
        with mock.patch.object(ax_helper, "is_supported", return_value=False):
            result = self.w.check()
        self.assertEqual(result.status, Status.ERROR)
        self.assertIn("not supported", result.detail)


if __name__ == "__main__":
    unittest.main()
