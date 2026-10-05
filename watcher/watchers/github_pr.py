"""
GitHub pull request status-check watcher plugin.

Polls a PR's status checks via the ``gh`` CLI (``gh pr view --json
headRefOid,...,statusCheckRollup``) and reports whether checks are still
running, have all passed, or any have failed. Uses the invoking user's
existing ``gh`` command-line authentication (``gh auth login``) — no
credentials are stored or sent by Watcher itself.

Requires the `GitHub CLI <https://cli.github.com/>`_ (``gh``) to be
installed and authenticated (``gh auth status``).
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from watcher.core.base import CheckResult, Status, Watcher
from watcher.core.registry import register

REQUEST_TIMEOUT_SECONDS = 20

# Conclusions/states that count as a failing check.
_FAILING_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"}
_FAILING_STATES = {"ERROR", "FAILURE"}

_PR_URL_RE = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")


def _run_gh_pr_view(pr_url: str) -> Dict[str, Any]:
    """
    Run ``gh pr view <pr_url> --json ...`` and parse its JSON output.

    Returns:
        The parsed JSON response body.

    Raises:
        RuntimeError: If the ``gh`` CLI is missing, not authenticated, or
            fails for any other reason (its stderr is included in the
            message).
    """
    fields = "headRefOid,number,title,state,url,statusCheckRollup"
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", fields],
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
        raise RuntimeError(f"gh pr view timed out after {REQUEST_TIMEOUT_SECONDS}s") from exc

    if proc.returncode != 0:
        stderr = proc.stderr.strip() or f"gh exited with code {proc.returncode}"
        raise RuntimeError(stderr)

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse gh output: {exc}") from exc


def _check_outcome(check: Dict[str, Any]) -> Tuple[bool, bool]:
    """
    Determine (is_resolved, is_passing) for a single statusCheckRollup entry.

    Handles both GitHub Actions ``CheckRun`` entries (``status``/
    ``conclusion``) and legacy commit ``StatusContext`` entries (``state``).

    Returns:
        A tuple of (is_resolved, is_passing) for the check.
    """
    if "state" in check:
        # Legacy StatusContext: state is one of SUCCESS/PENDING/ERROR/FAILURE.
        state = (check.get("state") or "").upper()
        if state == "PENDING":
            return False, False
        return True, state not in _FAILING_STATES

    # CheckRun: status is QUEUED/IN_PROGRESS/COMPLETED; conclusion set once COMPLETED.
    status = (check.get("status") or "").upper()
    if status != "COMPLETED":
        return False, False
    conclusion = (check.get("conclusion") or "").upper()
    return True, conclusion not in _FAILING_CONCLUSIONS


def _status_from_checks(rollup: List[Dict[str, Any]]) -> Tuple[Status, str]:
    """
    Translate a PR's statusCheckRollup into a (Status, detail) tuple.

    Returns:
        A tuple of (status, human-readable detail message).
    """
    if not rollup:
        return Status.IDLE, "No status checks found"

    total = len(rollup)
    pending = 0
    failing = 0
    for check in rollup:
        resolved, passing = _check_outcome(check)
        if not resolved:
            pending += 1
        elif not passing:
            failing += 1

    if pending:
        return Status.BUILDING, f"{pending}/{total} checks running"
    if failing:
        return Status.FAILURE, f"{failing}/{total} checks failed"
    return Status.SUCCESS, f"All {total} checks passed"


def _default_label_from_url(pr_url: str) -> str:
    """
    Derive a short ``owner/repo#123`` label from a GitHub PR URL.

    Returns:
        The derived label, or the raw URL if it doesn't match the expected
        pattern.
    """
    match = _PR_URL_RE.search(pr_url)
    if not match:
        return pr_url
    return f"{match.group('owner')}/{match.group('repo')}#{match.group('number')}"


@register
class GitHubPRWatcher(Watcher):
    """Watches a single GitHub pull request's status checks via the gh CLI."""

    watcher_type = "github_pr"

    def __init__(
        self,
        pr_url: str,
        watcher_id: Optional[str] = None,
        label: str = "",
        last_head_sha: Optional[str] = None,
        notes: str = "",
    ):
        """Initialize a GitHub PR watcher for the given PR URL."""
        self.pr_url = pr_url.rstrip("/")
        super().__init__(watcher_id=watcher_id, label=label, notes=notes)
        # The commit SHA we've already notified about; None means "we
        # haven't checked yet" so the very first check never notifies.
        self.last_head_sha = last_head_sha
        self._has_checked = last_head_sha is not None

    def default_label(self) -> str:
        """
        Derive a label like ``owner/repo#123`` from the PR URL.

        Returns:
            The derived label.
        """
        return _default_label_from_url(self.pr_url)

    @property
    def display_url(self) -> Optional[str]:
        """The GitHub PR URL, for opening in a browser."""
        return self.pr_url

    @classmethod
    def matches_url(cls, url: str) -> bool:
        """
        Whether a pasted URL looks like a GitHub pull request URL.

        Returns:
            True if the URL matches ``github.com/<owner>/<repo>/pull/<number>``.
        """
        return bool(_PR_URL_RE.search(url))

    def check(self) -> CheckResult:
        """
        Poll the PR's status checks and report status/newly-actionable state.

        Returns:
            The result of polling the PR's status checks.
        """
        try:
            data = _run_gh_pr_view(self.pr_url)
        except RuntimeError as exc:
            self.last_status = Status.ERROR
            self.last_detail = str(exc)
            return CheckResult(status=Status.ERROR, detail=str(exc))

        head_sha = data.get("headRefOid")
        status, detail = _status_from_checks(data.get("statusCheckRollup") or [])

        newly_actionable = False
        if (
            status.is_actionable
            and self._has_checked
            and head_sha is not None
            and head_sha != self.last_head_sha
        ):
            newly_actionable = True

        if head_sha is not None and status != Status.BUILDING:
            # Only "commit" the head SHA once checks have finished, so we
            # don't miss a transition if we happen to poll mid-run.
            self.last_head_sha = head_sha
        self._has_checked = True

        self.last_status = status
        self.last_detail = detail
        return CheckResult(status=status, detail=detail, newly_actionable=newly_actionable)

    def to_config(self) -> Dict[str, Any]:
        """
        Serialize this watcher's URL, id, label, and last head SHA.

        Returns:
            A dict suitable for JSON config storage.
        """
        return {
            "watcher_type": self.watcher_type,
            "id": self.id,
            "label": self.label,
            "pr_url": self.pr_url,
            "last_head_sha": self.last_head_sha,
            "notes": self.notes,
        }

    @classmethod
    def from_config(cls, data: Dict[str, Any]) -> "GitHubPRWatcher":
        """
        Reconstruct a GitHubPRWatcher from a dict produced by to_config().

        Returns:
            A new GitHubPRWatcher instance.
        """
        return cls(
            pr_url=data["pr_url"],
            watcher_id=data.get("id"),
            label=data.get("label", ""),
            last_head_sha=data.get("last_head_sha"),
            notes=data.get("notes", ""),
        )
