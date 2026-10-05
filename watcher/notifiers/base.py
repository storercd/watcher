"""
Abstract notifier interface.

New notifier types (Slack, SMS, etc.) should subclass :class:`Notifier` and
implement :meth:`notify`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Notifier(ABC):
    """A single notification channel (macOS native, Slack, SMS, ...)."""

    name: str = "base"

    @abstractmethod
    def notify(self, title: str, message: str) -> None:
        """
        Send a notification.

        Implementations should not raise on failure; log/print instead so one
        broken channel doesn't break the others.
        """
        raise NotImplementedError

    def nudge(self, has_unacknowledged: bool) -> None:
        """
        Re-assert attention for a still-unacknowledged result, called periodically.

        Unlike :meth:`notify` (fired once, on the moment a result becomes
        actionable), this is polled on an interval so a channel can re-nag
        if its first attempt had no effect (e.g. a Dock bounce request that
        was silently ignored because the app was frontmost at the time).
        Default is a no-op; most channels don't need this.
        """
        return None
