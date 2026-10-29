"""
GitHub Actions workflow run watcher plugin.

Polls a single workflow run via the ``gh`` CLI (``gh run view <run_id>
--repo owner/repo --json ...``) and reports whether it's still queued/running,
has succeeded, or has failed. Uses the invoking user's existing ``gh`` CLI
authentication (``gh auth login``) — no credentials are stored or sent by
Watcher itself.

Requires the `GitHub CLI <https://cli.github.com/>`_ (``gh``) to be
installed and authenticated (``gh auth status``).
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Dict, Optional, Tuple

from watcher.core.base import CheckResult, Status, Watcher
from watcher.core.gh_cli import run_gh
from watcher.core.registry import register

REQUEST_TIMEOUT_SECONDS = 20

# Conclusions that count as a failing run (mirrors github_pr.py's check-run
# conclusions, since `gh run view` reports the same vocabulary lowercased).
_FAILING_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"}

_RUN_URL_RE = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/actions/runs/(?P<run_id>\d+)")


def _parse_run_url(run_url: str) -> Tuple[str, str, str]:
    """
    Extract (owner, repo, run_id) from a GitHub Actions run URL.

    Returns:
        A tuple of (owner, repo, run_id).

    Raises:
        ValueError: If ``run_url`` doesn't match the expected
            ``github.com/<owner>/<repo>/actions/runs/<run_id>`` pattern.
    """
    match = _RUN_URL_RE.search(run_url)
    if not match:
        raise ValueError(f"Not a recognized GitHub Actions run URL: {run_url!r}")
    return match.group("owner"), match.group("repo"), match.group("run_id")


def _run_gh_run_view(owner: str, repo: str, run_id: str) -> Dict[str, Any]:
    """
    Run ``gh run view <run_id> --repo owner/repo --json ...`` and parse its output.

    Returns:
        The parsed JSON response body.

    Raises:
        RuntimeError: If the ``gh`` CLI is missing, not authenticated, or
            fails for any other reason (its stderr is included in the
            message).
    """
    fields = "status,conclusion,attempt,workflowName,displayTitle,url,jobs"
    try:
        proc = run_gh(
            "run", "view", run_id, "--repo", f"{owner}/{repo}", "--json", fields, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "gh CLI not found. Install it from https://cli.github.com/ and run `gh auth login`."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gh run view timed out after {REQUEST_TIMEOUT_SECONDS}s") from exc

    if proc.returncode != 0:
        stderr = proc.stderr.strip() or f"gh exited with code {proc.returncode}"
        raise RuntimeError(stderr)

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse gh output: {exc}") from exc


def _failed_jobs(data: Dict[str, Any]) -> list:
    """
    Names of jobs that have already completed with a failing conclusion.

    Returns:
        A list of failed job names.
    """
    return [
        job.get("name") or "unnamed job"
        for job in data.get("jobs") or []
        if (job.get("conclusion") or "").upper() in _FAILING_CONCLUSIONS
    ]


def _status_from_run(data: Dict[str, Any], fail_fast: bool = False) -> Tuple[Status, str, Optional[int]]:
    """
    Translate a workflow run's ``gh run view`` JSON into a (Status, detail, attempt) tuple.

    With ``fail_fast``, a still-running run is reported as a failure as soon
    as any of its jobs has already failed.

    Returns:
        A tuple of (status, human-readable detail message, run attempt number).
    """
    attempt = data.get("attempt")
    status_raw = (data.get("status") or "").lower()

    if status_raw != "completed":
        failed = _failed_jobs(data) if fail_fast else []
        if failed:
            return Status.FAILURE, f"Job failed: {', '.join(failed)} (run still in progress)", attempt
        label = status_raw.replace("_", " ") or "unknown"
        return Status.BUILDING, label.capitalize() or "Running", attempt

    conclusion = (data.get("conclusion") or "").upper()
    if conclusion in _FAILING_CONCLUSIONS:
        return Status.FAILURE, f"Run {conclusion.replace('_', ' ').lower()}", attempt
    return Status.SUCCESS, "Run succeeded", attempt


def _default_label_from_url(run_url: str) -> str:
    """
    Derive a short ``owner/repo run #12345`` label from a GitHub Actions run URL.

    Returns:
        The derived label, or the raw URL if it doesn't match the expected
        pattern.
    """
    try:
        owner, repo, run_id = _parse_run_url(run_url)
    except ValueError:
        return run_url
    return f"{owner}/{repo} run #{run_id}"


@register
class GitHubActionsRunWatcher(Watcher):
    """Watches a single GitHub Actions workflow run via the gh CLI."""

    watcher_type = "github_actions_run"

    def __init__(
        self,
        run_url: str,
        watcher_id: Optional[str] = None,
        label: str = "",
        last_attempt: Optional[int] = None,
        acknowledged_attempt: Optional[int] = None,
        notes: str = "",
        fail_fast: bool = True,
    ):
        """Initialize a GitHub Actions run watcher for the given run URL."""
        self.owner, self.repo, self.run_id = _parse_run_url(run_url)
        # Rebuild a clean canonical URL from the parsed parts rather than
        # storing the raw pasted text verbatim. Pasting can leave stray
        # surrounding/duplicated text around the actual URL (e.g. a
        # double-paste), and _parse_run_url's `search` (not `fullmatch`)
        # happily ignores that junk - but storing it as-is means
        # display_url later hands a mangled link to the browser.
        self.run_url = f"https://github.com/{self.owner}/{self.repo}/actions/runs/{self.run_id}"
        super().__init__(watcher_id=watcher_id, label=label, notes=notes)
        # The attempt number of the last completed run we've seen, used to
        # detect a genuinely new completion (e.g. after a manual re-run).
        self.last_attempt = last_attempt
        # Report the run as failed as soon as any job fails, even while other
        # jobs are still running.
        self.fail_fast = fail_fast
        # The attempt number the user has acknowledged (e.g. by clicking the
        # row). A completed attempt that doesn't match this is
        # "unacknowledged" and keeps notifying/highlighting across app
        # restarts until the user acknowledges it.
        self.acknowledged_attempt = acknowledged_attempt
        # In-memory only: whether this watcher has completed at least one
        # check() since the app started, used to re-alert once per restart
        # for a completion that's still unacknowledged.
        self._checked_since_start = False

    def default_label(self) -> str:
        """
        Derive a label like ``owner/repo run #12345`` from the run URL.

        Returns:
            The derived label.
        """
        return _default_label_from_url(self.run_url)

    @property
    def display_url(self) -> Optional[str]:
        """The GitHub Actions run URL, for opening in a browser."""
        return self.run_url

    @classmethod
    def matches_url(cls, url: str) -> bool:
        """
        Whether a pasted URL looks like a GitHub Actions run URL.

        Returns:
            True if the URL matches
            ``github.com/<owner>/<repo>/actions/runs/<run_id>``.
        """
        return bool(_RUN_URL_RE.search(url))

    def check(self) -> CheckResult:
        """
        Poll the workflow run and report its status/newly-actionable state.

        Returns:
            The result of polling the workflow run.
        """
        try:
            data = _run_gh_run_view(self.owner, self.repo, self.run_id)
        except RuntimeError as exc:
            self.last_status = Status.ERROR
            self.last_detail = str(exc)
            return CheckResult(status=Status.ERROR, detail=str(exc))

        status, detail, attempt = _status_from_run(data, self.fail_fast)

        unacknowledged = (
            status.is_actionable and attempt is not None and attempt != self.acknowledged_attempt
        )
        # Notify when the run newly completes (attempt advanced, e.g. a
        # re-run), or - on the first poll since the app started - when a
        # completion from a prior run is still unacknowledged, so restarting
        # re-alerts instead of staying silent forever.
        newly_actionable = unacknowledged and (
            attempt != self.last_attempt or not self._checked_since_start
        )
        self._checked_since_start = True

        if attempt is not None and status != Status.BUILDING:
            # Only "commit" the attempt number once the run has finished (or
            # failed fast), so we don't miss a transition if we poll mid-run.
            self.last_attempt = attempt

        self.last_status = status
        self.last_detail = detail
        self.unacknowledged = unacknowledged
        return CheckResult(
            status=status, detail=detail, newly_actionable=newly_actionable, unacknowledged=unacknowledged
        )

    def acknowledge(self) -> None:
        """Mark the current completed run as seen, clearing the unacknowledged state."""
        self.acknowledged_attempt = self.last_attempt
        self.unacknowledged = False

    def forget_acknowledgment(self) -> None:
        """Clear the acknowledged attempt so the last completion re-notifies."""
        self.acknowledged_attempt = None

    def to_config(self) -> Dict[str, Any]:
        """
        Serialize this watcher's URL, id, label, and attempt-tracking state.

        Returns:
            A dict suitable for JSON config storage.
        """
        return {
            "watcher_type": self.watcher_type,
            "id": self.id,
            "label": self.label,
            "run_url": self.run_url,
            "last_attempt": self.last_attempt,
            "acknowledged_attempt": self.acknowledged_attempt,
            "notes": self.notes,
            "fail_fast": self.fail_fast,
        }

    @classmethod
    def from_config(cls, data: Dict[str, Any]) -> "GitHubActionsRunWatcher":
        """
        Reconstruct a GitHubActionsRunWatcher from a dict produced by to_config().

        Returns:
            A new GitHubActionsRunWatcher instance.
        """
        return cls(
            run_url=data["run_url"],
            watcher_id=data.get("id"),
            label=data.get("label", ""),
            last_attempt=data.get("last_attempt"),
            acknowledged_attempt=data.get("acknowledged_attempt"),
            notes=data.get("notes", ""),
            fail_fast=data.get("fail_fast", True),
        )
