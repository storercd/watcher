"""
Long-term GitHub pull request / issue watcher with a configurable trigger.

Unlike :class:`~watcher.watchers.github_pr.GitHubPRWatcher` (a short-lived
"tell me when CI finishes" watcher), this one is meant to sit for hours or
days and alert on one chosen event:

* ``checks``   - PR status checks finished (delegates to the PR watcher).
* ``closed``   - the PR was merged/closed or the issue was closed.
* ``activity`` - anything happened (comment, label, review, push, ...).

It uses the ``gh`` CLI, like the other GitHub watchers, so no credentials are
stored by Watcher itself.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Dict, Optional

from watcher.core.base import CheckResult, Status, Watcher
from watcher.core.gh_cli import gh_command
from watcher.core.registry import register
from watcher.watchers.github_pr import GitHubPRWatcher

REQUEST_TIMEOUT_SECONDS = 20

TRIGGER_CHECKS = "checks"
TRIGGER_CLOSED = "closed"
TRIGGER_ACTIVITY = "activity"
DEFAULT_TRIGGER = TRIGGER_CLOSED
PR_TRIGGERS = (TRIGGER_CLOSED, TRIGGER_ACTIVITY, TRIGGER_CHECKS)
ISSUE_TRIGGERS = (TRIGGER_CLOSED, TRIGGER_ACTIVITY)

_ITEM_URL_RE = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?P<kind>pull|issues)/(?P<number>\d+)")


def parse_item_url(url: str) -> Optional[Dict[str, str]]:
    """
    Parse a GitHub PR or issue URL.

    Returns:
        A dict with ``owner``, ``repo``, ``kind`` (``pull`` or ``issues``)
        and ``number``, or None if the URL isn't a PR/issue URL.
    """
    match = _ITEM_URL_RE.search(url)
    return match.groupdict() if match else None


def triggers_for_url(url: str) -> tuple:
    """
    List the triggers that make sense for a PR or issue URL.

    Returns:
        The valid trigger names, or an empty tuple for an unrecognized URL.
    """
    parsed = parse_item_url(url)
    if parsed is None:
        return ()
    return PR_TRIGGERS if parsed["kind"] == "pull" else ISSUE_TRIGGERS


def _normalize_url(url: str) -> str:
    parsed = parse_item_url(url)
    if parsed is None:
        return url.rstrip("/")
    return f"https://github.com/{parsed['owner']}/{parsed['repo']}/{parsed['kind']}/{parsed['number']}"


def _run_gh_view(kind: str, url: str) -> Dict[str, Any]:
    """
    Run ``gh pr|issue view <url> --json state,updatedAt,title``.

    Returns:
        The parsed JSON response body.

    Raises:
        RuntimeError: If ``gh`` is missing, unauthenticated, or fails.
    """
    subcommand = "pr" if kind == "pull" else "issue"
    try:
        proc = subprocess.run(
            gh_command(subcommand, "view", url, "--json", "state,updatedAt,title"),
            capture_output=True,
            text=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "gh CLI not found. Install it from https://cli.github.com/ and run `gh auth login`."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gh {subcommand} view timed out after {REQUEST_TIMEOUT_SECONDS}s") from exc

    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh exited with code {proc.returncode}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse gh output: {exc}") from exc


@register
class GitHubItemWatcher(Watcher):
    """Watches one GitHub PR or issue for a configurable event over a long period."""

    watcher_type = "github_item"

    def __init__(
        self,
        url: str,
        watcher_id: Optional[str] = None,
        label: str = "",
        notes: str = "",
        trigger: Optional[str] = None,
        last_key: Optional[str] = None,
        acknowledged_key: Optional[str] = None,
        last_head_sha: Optional[str] = None,
        acknowledged_head_sha: Optional[str] = None,
    ):
        """
        Initialize the watcher.

        Raises:
            ValueError: If ``url`` isn't a PR/issue URL or ``trigger`` isn't
                valid for it.
        """
        parsed = parse_item_url(url)
        if parsed is None:
            raise ValueError("Not a GitHub pull request or issue URL")
        self.url = _normalize_url(url)
        self.kind = parsed["kind"]
        self.trigger = trigger or DEFAULT_TRIGGER
        if self.trigger not in triggers_for_url(self.url):
            raise ValueError(f"Trigger {self.trigger!r} is not valid for this URL")
        self._parsed = parsed
        super().__init__(watcher_id=watcher_id, label=label, notes=notes)
        self.category = "long_term"
        # Trigger-specific marker last seen / acknowledged (state or updatedAt).
        self.last_key = last_key
        self.acknowledged_key = acknowledged_key
        self._checks = (
            GitHubPRWatcher(
                self.url,
                last_head_sha=last_head_sha,
                acknowledged_head_sha=acknowledged_head_sha,
            )
            if self.trigger == TRIGGER_CHECKS
            else None
        )
        self._checked_since_start = False

    def default_label(self) -> str:
        """
        Derive a label like ``owner/repo#123``.

        Returns:
            The derived label.
        """
        parsed = getattr(self, "_parsed", None)
        if parsed is None:
            return self.url
        return f"{parsed['owner']}/{parsed['repo']}#{parsed['number']}"

    @property
    def display_url(self) -> Optional[str]:
        """The PR/issue URL, for opening in a browser."""
        return self.url

    @classmethod
    def matches_url(cls, url: str) -> bool:
        """
        Never auto-detected: long-term watchers are created explicitly.

        Returns:
            Always False, so PR URLs keep resolving to the immediate watcher.
        """
        return False

    @property
    def last_head_sha(self) -> Optional[str]:
        """Head SHA tracked by the ``checks`` trigger, if any."""
        return self._checks.last_head_sha if self._checks else None

    @property
    def acknowledged_head_sha(self) -> Optional[str]:
        """Head SHA acknowledged under the ``checks`` trigger, if any."""
        return self._checks.acknowledged_head_sha if self._checks else None

    def check(self) -> CheckResult:
        """
        Poll the item and evaluate the configured trigger.

        Returns:
            The result of the poll.
        """
        if self._checks is not None:
            result = self._checks.check()
            self.last_status = result.status
            self.last_detail = result.detail
            self.unacknowledged = result.unacknowledged
            return result

        try:
            data = _run_gh_view(self.kind, self.url)
        except RuntimeError as exc:
            self.last_status = Status.ERROR
            self.last_detail = str(exc)
            return CheckResult(status=Status.ERROR, detail=str(exc))

        state = (data.get("state") or "").upper()
        if self.trigger == TRIGGER_CLOSED:
            return self._evaluate_closed(state)
        return self._evaluate_activity(data.get("updatedAt"))

    def _evaluate_closed(self, state: str) -> CheckResult:
        if state == "MERGED":
            return self._finish(Status.SUCCESS, "Merged", key=state)
        if state == "CLOSED":
            if self.kind == "pull":
                return self._finish(Status.FAILURE, "Closed without merging", key=state)
            return self._finish(Status.SUCCESS, "Closed", key=state)
        return self._finish(Status.IDLE, "Open - waiting for merge/close" if self.kind == "pull" else "Open", key=None)

    def _evaluate_activity(self, updated_at: Optional[str]) -> CheckResult:
        if not updated_at:
            return self._finish(Status.IDLE, "No activity data", key=None)
        if self.acknowledged_key is None:
            # First look: this is the baseline, not news.
            self.acknowledged_key = updated_at
            self.last_key = updated_at
        if updated_at != self.acknowledged_key:
            return self._finish(Status.SUCCESS, f"New activity (updated {updated_at})", key=updated_at)
        return self._finish(Status.IDLE, "No new activity", key=updated_at)

    def _finish(self, status: Status, detail: str, key: Optional[str]) -> CheckResult:
        unacknowledged = status.is_actionable and key is not None and key != self.acknowledged_key
        newly_actionable = unacknowledged and (key != self.last_key or not self._checked_since_start)
        self._checked_since_start = True
        if key is not None:
            self.last_key = key
        self.last_status = status
        self.last_detail = detail
        self.unacknowledged = unacknowledged
        return CheckResult(
            status=status, detail=detail, newly_actionable=newly_actionable, unacknowledged=unacknowledged
        )

    def acknowledge(self) -> None:
        """Mark the current result as seen so only later events alert again."""
        if self._checks is not None:
            self._checks.acknowledge()
        else:
            self.acknowledged_key = self.last_key
        self.unacknowledged = False

    def forget_acknowledgment(self) -> None:
        """Re-arm one-shot triggers so a finished result re-notifies after a restart."""
        if self._checks is not None:
            self._checks.forget_acknowledgment()
        elif self.trigger == TRIGGER_CLOSED:
            self.acknowledged_key = None

    def to_config(self) -> Dict[str, Any]:
        """
        Serialize this watcher for JSON config storage.

        Returns:
            A dict suitable for JSON config storage.
        """
        return {
            "watcher_type": self.watcher_type,
            "id": self.id,
            "label": self.label,
            "url": self.url,
            "trigger": self.trigger,
            "last_key": self.last_key,
            "acknowledged_key": self.acknowledged_key,
            "last_head_sha": self.last_head_sha,
            "acknowledged_head_sha": self.acknowledged_head_sha,
            "notes": self.notes,
        }

    @classmethod
    def from_config(cls, data: Dict[str, Any]) -> "GitHubItemWatcher":
        """
        Reconstruct a watcher from a dict produced by to_config().

        Returns:
            A new GitHubItemWatcher instance.
        """
        return cls(
            url=data["url"],
            watcher_id=data.get("id"),
            label=data.get("label", ""),
            notes=data.get("notes", ""),
            trigger=data.get("trigger"),
            last_key=data.get("last_key"),
            acknowledged_key=data.get("acknowledged_key"),
            last_head_sha=data.get("last_head_sha"),
            acknowledged_head_sha=data.get("acknowledged_head_sha"),
        )
