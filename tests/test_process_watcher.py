"""Unit tests for the running-process watcher and terminal-command listing."""

import subprocess
import sys
import time
import unittest
from unittest import mock

from watcher.core import processes
from watcher.core.base import Status
from watcher.core.registry import detect_watcher_class
from watcher.watchers.process import ProcessWatcher, build_url

STARTED = 1_700_000_000.0
URL = build_url(4242, STARTED, "python sync.py ehs cheer")


class ProcessWatcherTests(unittest.TestCase):
    """The "process exited means done" state machine."""

    def setUp(self):
        """Setup."""
        self.w = ProcessWatcher(URL)

    def check(self, start_time):
        """Run one poll against a faked ``ps`` start time (None means not running)."""
        with mock.patch.object(processes, "process_start_time", return_value=start_time):
            return self.w.check()

    def test_url_parsing_and_detection(self):
        """Pid, start and command round-trip and the type is auto-detected."""
        self.assertEqual((self.w.pid, self.w.started, self.w.command), (4242, STARTED, "python sync.py ehs cheer"))
        self.assertEqual(self.w.display_url, URL)
        self.assertIs(detect_watcher_class(URL), ProcessWatcher)
        self.assertIsNone(detect_watcher_class("proc://abc?started=1"))
        self.assertIsNone(detect_watcher_class("proc://12"))
        self.assertIsNone(detect_watcher_class("https://example.com"))
        self.assertEqual(self.w.label, "Command: python sync.py ehs cheer")

    def test_running_is_building(self):
        """A live process with the recorded start time is building."""
        result = self.check(STARTED)
        self.assertEqual(result.status, Status.BUILDING)
        self.assertFalse(result.newly_actionable)

    def test_exit_notifies_once_until_acknowledged(self):
        """Exit gives one notification, a sticky success row, then idle after acknowledging."""
        self.check(STARTED)
        done = self.check(None)
        self.assertEqual(done.status, Status.SUCCESS)
        self.assertTrue(done.newly_actionable and done.unacknowledged)
        again = self.check(None)
        self.assertEqual(again.status, Status.SUCCESS)
        self.assertFalse(again.newly_actionable)
        self.w.acknowledge()
        self.assertEqual(self.check(None).status, Status.IDLE)

    def test_reused_pid_counts_as_finished(self):
        """A different process now holding the pid means the watched one exited."""
        self.assertEqual(self.check(STARTED + 500).status, Status.SUCCESS)

    def test_already_gone_when_added(self):
        """A process that exited before the first poll still notifies."""
        result = self.check(None)
        self.assertTrue(result.newly_actionable)

    def test_restart_renotifies_unacknowledged_completion(self):
        """A completion saved but never acknowledged notifies again after a restart."""
        self.check(None)
        restored = ProcessWatcher.from_config(self.w.to_config())
        with mock.patch.object(processes, "process_start_time", return_value=None):
            self.assertTrue(restored.check().newly_actionable)
        restored.acknowledge()
        again = ProcessWatcher.from_config(restored.to_config())
        with mock.patch.object(processes, "process_start_time", return_value=None):
            self.assertFalse(again.check().newly_actionable)


class ProcessesModuleTests(unittest.TestCase):
    """Real ``ps`` lookups against a child process of the test run."""

    def test_start_time_of_live_and_exited_process(self):
        """A live pid reports its start time; an exited, reaped one reports None."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            started = processes.process_start_time(child.pid)
            self.assertIsNotNone(started)
            self.assertLess(abs(started - time.time()), 30)
        finally:
            child.kill()
            child.wait()
        self.assertIsNone(processes.process_start_time(child.pid))

    def test_is_shell(self):
        """Shells, including login shells shown as ``-zsh``, are recognized."""
        self.assertTrue(processes._is_shell("/bin/zsh -il"))
        self.assertTrue(processes._is_shell("-zsh"))
        self.assertFalse(processes._is_shell("python sync.py"))

    def test_lists_foreground_children_of_shells(self):
        """Only tty-attached foreground children of a shell are listed."""
        ps_output = "\n".join(
            [
                "100 1 ttys006 Ss+ Thu Oct 29 06:00:00 2026 /bin/zsh -i",
                "200 100 ttys006 S+ Thu Oct 29 07:44:21 2026 python sync.py ehs cheer",
                "201 200 ttys006 S+ Thu Oct 29 07:44:22 2026 python worker.py",
                "300 100 ttys006 S Thu Oct 29 07:00:00 2026 sleep 100",
                "400 1 ?? S Thu Oct 29 07:00:00 2026 daemon",
            ]
        )
        with mock.patch.object(processes, "_run", return_value=ps_output):
            with mock.patch.object(processes, "_working_directories", return_value={200: "/work"}):
                result = processes.list_terminal_commands()
        self.assertEqual([item["pid"] for item in result], [200])
        self.assertEqual(result[0]["command"], "python sync.py ehs cheer")
        self.assertEqual(result[0]["cwd"], "/work")
        self.assertEqual(result[0]["tty"], "ttys006")
