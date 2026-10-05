"""
Stub notifier for future remote channels (SMS, Slack, etc.).

Used when the user is in "Away" mode. For now it just logs a TODO so the
plumbing (mode selection -> routing) is in place before those integrations
are built.
"""

from __future__ import annotations

from watcher.notifiers.base import Notifier


class AwayNotifier(Notifier):
    """Placeholder for away-from-desk notifications (SMS/Slack/etc.)."""

    name = "away"

    def notify(self, title: str, message: str) -> None:
        """Log a TODO placeholder instead of sending a real away notification."""
        print(f"[away-notifier] TODO: send SMS/Slack notification -> {title}: {message}")
