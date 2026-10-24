"""
Running-app progress watcher plugin (macOS).

Watches another app's progress bar through the Accessibility API (via the
``watcher-ax`` helper). Accepts ``app://<bundle-id>`` with an optional
``?window=<title substring>`` filter, since a bundle id alone is too coarse
for apps like ``org.python.python`` that host many unrelated windows.
``&dock=1`` additionally reads a progress bar drawn on the app's Dock icon, for
apps (like Lightroom Classic) that draw their progress without exposing it to
accessibility; this works even while the app is minimized.

The rule is deliberately simple and app-agnostic: a progress bar that was
seen and then disappears means the app is ready.

* progress bar present -> building
* bar seen, then absent for ``MISSING_POLLS_TO_FINISH`` polls -> success
* app not running -> idle (a quit is never reported as "done")
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, quote, unquote, urlsplit

from watcher.core import ax_helper
from watcher.core.base import CheckResult, Status, Watcher
from watcher.core.registry import register

URL_PREFIX = "app://"
# Require the bar to stay gone for consecutive polls so a momentary glitch in
# the target's accessibility tree isn't reported as completion.
MISSING_POLLS_TO_FINISH = 2


def _parse_target(text: str) -> tuple:
    """
    Split ``app://<bundle-id>[?window=<title>][&dock=1]`` into its parts.

    Returns:
        A tuple of (bundle_id, window_title, dock).

    Raises:
        ValueError: If the text is not an app URL with a bundle id.
    """
    text = (text or "").strip()
    if not text.lower().startswith(URL_PREFIX):
        raise ValueError(f"Not a recognized app URL: {text!r}")
    parts = urlsplit(text)
    bundle_id = unquote(parts.netloc or parts.path.lstrip("/"))
    if not bundle_id:
        raise ValueError("App URL is missing a bundle id")
    query = parse_qs(parts.query)
    return bundle_id, query.get("window", [""])[0], query.get("dock", [""])[0] == "1"


def build_url(bundle_id: str, window_title: str = "", dock: bool = False) -> str:
    """
    Build the ``app://`` URL for a bundle id, optional window title and Dock-icon option.

    Returns:
        The URL accepted by :class:`AppProgressWatcher`.
    """
    params = []
    if window_title:
        params.append(f"window={quote(window_title, safe='')}")
    if dock:
        params.append("dock=1")
    url = f"{URL_PREFIX}{quote(bundle_id, safe='')}"
    return f"{url}?{'&'.join(params)}" if params else url


def _describe_progress(indicators: list) -> str:
    """
    Summarize indicator values as ``Progress 8/50 (16%)`` where possible.

    Returns:
        A short human-readable summary.
    """
    for item in indicators:
        value = item.get("value")
        if value is None:
            continue
        low, high = item.get("min", 0), item.get("max", 1)
        if high > low:
            pct = round(100 * (value - low) / (high - low))
            return f"Progress {pct}%"
    return "In progress"


@register
class AppProgressWatcher(Watcher):
    """Watches a running macOS app's progress bar for it to finish."""

    watcher_type = "app_progress"

    def __init__(
        self,
        url: str,
        watcher_id: Optional[str] = None,
        label: str = "",
        last_completed: Optional[str] = None,
        acknowledged_completed: Optional[str] = None,
        notes: str = "",
    ):
        """Initialize from an ``app://<bundle-id>[?window=<title>]`` URL."""
        self.bundle_id, self.window_title, self.dock = _parse_target(url)
        super().__init__(watcher_id=watcher_id, label=label, notes=notes)
        self.last_completed = last_completed
        self.acknowledged_completed = acknowledged_completed
        self._seen_progress = False
        self._missing_polls = 0
        self._notified_completed: Optional[str] = None

    def default_label(self) -> str:
        """
        Name the watcher after the window title, or else the bundle id.

        Returns:
            The derived label.
        """
        return f"App: {self.window_title or self.bundle_id}"

    @property
    def display_url(self) -> Optional[str]:
        """The ``app://`` URL, which the GUI uses for editing and to switch to the app."""
        return build_url(self.bundle_id, self.window_title, self.dock)

    @classmethod
    def matches_url(cls, url: str) -> bool:
        """
        Whether the text is an ``app://`` URL.

        Returns:
            True for ``app://<bundle-id>`` forms.
        """
        try:
            _parse_target(url)
        except ValueError:
            return False
        return True

    def _fail(self, status: Status, detail: str) -> CheckResult:
        self.last_status, self.last_detail = status, detail
        return CheckResult(status=status, detail=detail)

    def check(self) -> CheckResult:
        """
        Read the app's progress bar and apply the "bar disappeared" rule.

        Returns:
            The result of inspecting the app.
        """
        if not ax_helper.is_supported():
            return self._fail(Status.ERROR, ax_helper.unsupported_message())
        try:
            reading = ax_helper.read_progress(self.bundle_id, self.window_title, self.dock)
        except ax_helper.AxHelperError as exc:
            return self._fail(Status.ERROR, str(exc))

        indicators = reading.get("progress") or []
        if not reading.get("running"):
            self._seen_progress, self._missing_polls = False, 0
            status, detail = Status.IDLE, "App is not running"
        elif indicators:
            self._seen_progress, self._missing_polls = True, 0
            status, detail = Status.BUILDING, _describe_progress(indicators)
        elif self._seen_progress:
            self._missing_polls += 1
            if self._missing_polls >= MISSING_POLLS_TO_FINISH:
                self._seen_progress, self._missing_polls = False, 0
                self.last_completed = str(time.time_ns())
                status, detail = Status.SUCCESS, "Progress finished - ready"
            else:
                status, detail = Status.BUILDING, "Progress bar gone - confirming"
        else:
            status, detail = Status.IDLE, "No progress bar showing"

        unacknowledged = (
            self.last_completed is not None
            and self.last_completed != self.acknowledged_completed
            and status in (Status.IDLE, Status.SUCCESS)
        )
        if unacknowledged:
            status, detail = Status.SUCCESS, "Progress finished - ready"
        # Notify once per completion; a fresh process re-notifies a completion
        # that is still unacknowledged (like a restart with a pending result).
        newly_actionable = unacknowledged and self.last_completed != self._notified_completed
        if unacknowledged:
            self._notified_completed = self.last_completed
        self.last_status, self.last_detail, self.unacknowledged = status, detail, unacknowledged
        return CheckResult(
            status=status, detail=detail, newly_actionable=newly_actionable, unacknowledged=unacknowledged
        )

    def acknowledge(self) -> None:
        """Mark the last completion as seen."""
        self.acknowledged_completed = self.last_completed
        self.unacknowledged = False

    def forget_acknowledgment(self) -> None:
        """Clear the acknowledged marker so the last completion re-notifies."""
        self.acknowledged_completed = None

    def to_config(self) -> Dict[str, Any]:
        """
        Serialize for JSON config storage.

        Returns:
            A dict suitable for JSON config storage.
        """
        return {
            "watcher_type": self.watcher_type,
            "id": self.id,
            "label": self.label,
            "url": build_url(self.bundle_id, self.window_title, self.dock),
            "last_completed": self.last_completed,
            "acknowledged_completed": self.acknowledged_completed,
            "notes": self.notes,
        }

    @classmethod
    def from_config(cls, data: Dict[str, Any]) -> "AppProgressWatcher":
        """
        Reconstruct from a dict produced by to_config().

        Returns:
            A new AppProgressWatcher instance.
        """
        return cls(
            url=data["url"],
            watcher_id=data.get("id"),
            label=data.get("label", ""),
            last_completed=data.get("last_completed"),
            acknowledged_completed=data.get("acknowledged_completed"),
            notes=data.get("notes", ""),
        )
