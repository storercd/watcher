"""
Core status types and the abstract Watcher plugin interface.

Any new watcher type (Jenkins, AWS Step Functions, filesystem, etc.) should
subclass :class:`Watcher` and implement :meth:`Watcher.check`. The GUI and
scheduler only depend on this interface, so new watcher types can be added
without touching core/gui code.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class Status(str, Enum):
    """Normalized status for any watched item."""

    UNKNOWN = "unknown"
    IDLE = "idle"
    BUILDING = "building"
    SUCCESS = "success"
    FAILURE = "failure"
    ERROR = "error"  # watcher failed to reach the target (network error, etc.)

    @property
    def is_actionable(self) -> bool:
        """Whether this status represents something the user should look at."""
        return self in (Status.SUCCESS, Status.FAILURE)


@dataclass
class CheckResult:
    """The outcome of a single :meth:`Watcher.check` call."""

    status: Status
    detail: str = ""
    newly_actionable: bool = False
    unacknowledged: bool = False
    checked_at: float = field(default_factory=time.time)


class Watcher(ABC):
    """
    Abstract base class for all watcher plugins.

    Subclasses must implement :meth:`check`, :meth:`to_config`, and expose a
    class-level ``watcher_type`` string used to identify the plugin in the
    saved JSON config so it can be reconstructed on startup.
    """

    watcher_type: str = "base"

    def __init__(self, watcher_id: Optional[str] = None, label: str = "", notes: str = ""):
        """Initialize a watcher with an id (generated if omitted), a label, and optional notes."""
        self.id = watcher_id or str(uuid.uuid4())
        self.label = label or self.default_label()
        # Freeform text for "why this mattered" / follow-up reminders; viewed/edited
        # by clicking the notes icon in the GUI.
        self.notes = notes
        self.last_status: Status = Status.UNKNOWN
        self.last_checked: Optional[float] = None
        self.last_detail: str = ""
        # Whether the most recent actionable (success/failure) result is still
        # pending the user's acknowledgment (e.g. a click in the GUI). The GUI
        # keeps that row highlighted, and notifications keep firing on
        # restart, until :meth:`acknowledge` is called.
        self.unacknowledged: bool = False

    def acknowledge(self) -> None:
        """
        Mark the current actionable result as seen by the user.

        Subclasses that track completions more specifically (e.g. by build
        number) should override this to persist exactly which result was
        acknowledged, so a later genuinely-new completion still notifies.
        """
        self.unacknowledged = False

    @property
    def display_url(self) -> Optional[str]:
        """
        The URL this watcher points at, for opening in a browser (e.g. via a
        double-click in the GUI). Subclasses should override this with their
        watched URL; returns None if there is nothing sensible to open.
        """
        return None

    def default_label(self) -> str:
        """
        Fallback label if the user didn't provide one.

        Returns:
            The default label to use for this watcher.
        """
        return self.watcher_type

    @classmethod
    def matches_url(cls, url: str) -> bool:
        """
        Whether a pasted URL looks like something this watcher type can watch.

        Used by the single "Add Watcher" dialog to auto-detect the watcher
        type from a pasted URL instead of asking the user to pick a type up
        front. Subclasses should override this with a type-specific URL
        pattern check.

        Returns:
            True if this watcher type recognizes the URL format.
        """
        return False

    @abstractmethod
    def check(self) -> CheckResult:
        """
        Poll the watched target and return a :class:`CheckResult`.

        Implementations should update ``self.last_status``/``self.last_checked``
        themselves (the scheduler does not do this for you) so repeated calls
        can compare against prior state to compute ``newly_actionable``.

        Returns:
            The result of polling the watched target.
        """
        raise NotImplementedError

    @abstractmethod
    def to_config(self) -> Dict[str, Any]:
        """Serialize this watcher's configuration (not runtime state) to a dict."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def from_config(cls, data: Dict[str, Any]) -> "Watcher":
        """Reconstruct a watcher instance from a dict produced by to_config()."""
        raise NotImplementedError
