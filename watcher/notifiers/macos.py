"""
macOS native notification via ``osascript``.

Uses ``display notification`` so no extra dependency (pync, terminal-notifier)
is required beyond what ships with macOS.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time

from watcher.notifiers.base import Notifier

logger = logging.getLogger("watcher.notifiers.macos")


def _escape_for_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


class MacOSNotifier(Notifier):
    """Fires a native macOS banner notification via AppleScript/osascript."""

    name = "macos"

    def notify(self, title: str, message: str) -> None:
        """Display a native macOS banner notification via osascript."""
        if shutil.which("osascript") is None:
            print(f"[macos-notifier] osascript not found; {title}: {message}")
            return
        script = (
            f'display notification "{_escape_for_applescript(message)}" '
            f'with title "{_escape_for_applescript(title)}"'
        )
        try:
            start = time.monotonic()
            subprocess.run(
                ["osascript", "-e", script],
                check=False,
                capture_output=True,
                timeout=5,
            )
            logger.debug("osascript notification took %.3fs", time.monotonic() - start)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[macos-notifier] failed to send notification: {exc}")
