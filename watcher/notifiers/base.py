"""Abstract notifier interface.

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
        """Send a notification. Implementations should not raise on failure;
        log/print instead so one broken channel doesn't break the others."""
        raise NotImplementedError
