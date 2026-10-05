"""
Routes notifications to the right notifier(s) based on the current mode.

Pluggable: add new modes/notifiers here without touching callers, which only
ever call ``router.notify(title, message)``.
"""

from __future__ import annotations

from typing import Dict, List

from watcher.notifiers.away import DEFAULT_NTFY_SERVER, AwayNotifier
from watcher.notifiers.base import Notifier
from watcher.notifiers.macos import MacOSNotifier

AT_DESK = "At Desk"
AWAY = "Away"

MODES = (AT_DESK, AWAY)


class NotificationRouter:
    """Holds the current mode and dispatches to the matching notifier(s)."""

    def __init__(self, mode: str = AT_DESK, ntfy_server: str = DEFAULT_NTFY_SERVER, ntfy_topic: str = ""):
        """Initialize the router with a starting mode, defaulting to At Desk."""
        self.mode = mode if mode in MODES else AT_DESK
        self.away_notifier = AwayNotifier(server=ntfy_server, topic=ntfy_topic)
        self._notifiers: Dict[str, List[Notifier]] = {
            AT_DESK: [MacOSNotifier()],
            AWAY: [self.away_notifier],
        }

    def configure_away(self, server: str, topic: str) -> None:
        """Update the ntfy server/topic used by the Away notifier."""
        self.away_notifier.configure(server=server, topic=topic)

    def set_mode(self, mode: str) -> None:
        """
        Switch the active notification mode, validating it's a known mode.

        Raises:
            ValueError: If ``mode`` is not one of the known MODES.
        """
        if mode not in MODES:
            raise ValueError(f"Unknown mode: {mode!r}")
        self.mode = mode

    def notify(self, title: str, message: str) -> None:
        """Dispatch a notification to every notifier registered for the current mode."""
        for notifier in self._notifiers.get(self.mode, []):
            notifier.notify(title, message)

    def nudge(self, has_unacknowledged: bool) -> None:
        """Periodically re-assert attention via every notifier registered for the current mode."""
        for notifier in self._notifiers.get(self.mode, []):
            notifier.nudge(has_unacknowledged)
