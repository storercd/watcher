"""
macOS native notification via ``osascript``, plus a bouncing Dock icon.

Uses ``display notification`` so no extra dependency (pync, terminal-notifier)
is required for the banner itself beyond what ships with macOS. The Dock
bounce uses PyObjC's ``NSApplication.requestUserAttention_`` so a finished
watcher keeps bouncing the Dock icon - much harder to miss than a silent,
auto-dismissing banner - until the user actually clicks into the app.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time

from watcher.notifiers.base import Notifier

logger = logging.getLogger("watcher.notifiers.macos")

# Name of a built-in macOS sound asset (see /System/Library/Sounds) to play
# alongside the banner. "Basso" is one of the more attention-grabbing ones.
NOTIFICATION_SOUND = "Basso"


def _escape_for_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _is_app_active() -> bool:
    """
    Whether this app is currently the frontmost/active application.

    Conservatively returns ``True`` if PyObjC isn't available or the check
    fails, so callers skip re-bouncing rather than risk bouncing while the
    user is already looking at the app.

    Returns:
        Whether the app is currently active/frontmost.
    """
    try:
        from AppKit import NSApp, NSApplication  # noqa: PLC0415 - optional, lazy dep
    except ImportError:
        return True
    try:
        app = NSApp() or NSApplication.sharedApplication()
        return bool(app.isActive())
    except Exception as exc:  # noqa: BLE001 - never let this check break notifications
        logger.debug("Active-app check failed: %s", exc)
        return True


def _bounce_dock_icon() -> None:
    """
    Request a continuous Dock icon bounce via PyObjC.

    Uses ``NSCriticalRequest`` so the icon keeps bouncing until the app
    becomes active (the user clicks it), instead of a single polite bounce.
    Imports PyObjC lazily so platforms/environments without it still work -
    the bounce is a nice-to-have, not a hard requirement for notifications.
    """
    try:
        from AppKit import NSApp, NSApplication, NSCriticalRequest  # noqa: PLC0415 - optional, lazy dep
    except ImportError:
        logger.debug("PyObjC not available; skipping Dock bounce")
        return
    try:
        app = NSApp() or NSApplication.sharedApplication()
        app.requestUserAttention_(NSCriticalRequest)
    except Exception as exc:  # noqa: BLE001 - never let a bounce failure break notifications
        logger.debug("Dock bounce request failed: %s", exc)


class MacOSNotifier(Notifier):
    """Fires a native macOS banner notification (with sound) and bounces the Dock icon."""

    name = "macos"

    def notify(self, title: str, message: str) -> None:
        """Display a native macOS banner notification via osascript and bounce the Dock icon."""
        _bounce_dock_icon()
        if shutil.which("osascript") is None:
            print(f"[macos-notifier] osascript not found; {title}: {message}")
            return
        script = (
            f'display notification "{_escape_for_applescript(message)}" '
            f'with title "{_escape_for_applescript(title)}" '
            f'sound name "{NOTIFICATION_SOUND}"'
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

    def nudge(self, has_unacknowledged: bool) -> None:
        """
        Re-request the Dock bounce if something is still unacknowledged and we're backgrounded.

        ``requestUserAttention_`` is a no-op while the app is frontmost (e.g.
        it was active at the moment a watcher finished), so a bounce
        requested at completion time can be silently swallowed. Polling this
        on an interval catches the case where the user later switches away
        without acknowledging: as soon as we're backgrounded, the bounce
        request finally takes effect.
        """
        if not has_unacknowledged:
            return
        if _is_app_active():
            return
        _bounce_dock_icon()
