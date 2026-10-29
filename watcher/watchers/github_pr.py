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
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from watcher.core.base import CheckResult, Status, Watcher
from watcher.core.gh_cli import run_gh
from watcher.core.registry import register

REQUEST_TIMEOUT_SECONDS = 20

# Conclusions/states that count as a failing check.
_FAILING_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"}
_FAILING_STATES = {"ERROR", "FAILURE"}

# A Copilot session with no finish event after this long is treated as stalled
# rather than masking a failure forever.
COPILOT_SESSION_MAX_AGE = timedelta(hours=3)

_COPILOT_START_EVENT = "copilot_work_started"

_PR_URL_RE = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")


def _normalize_pr_url(pr_url: str) -> str:
    """
    Rebuild a clean canonical PR URL from whatever was parsed out of it.

    Pasting can leave stray surrounding/duplicated text around the actual
    URL (e.g. a double-paste); ``_PR_URL_RE.search`` happily ignores that
    junk when parsing, but storing the raw text as-is means ``display_url``
    later hands a mangled link to the browser. Falls back to the
    slash-trimmed raw input if it doesn't match the expected pattern.

    Returns:
        The canonical ``github.com/<owner>/<repo>/pull/<number>`` URL, or
        the raw input (with any trailing slash stripped) if it isn't
        recognized.
    """
    stripped = pr_url.rstrip("/")
    match = _PR_URL_RE.search(stripped)
    if not match:
        return stripped
    return f"https://github.com/{match.group('owner')}/{match.group('repo')}/pull/{match.group('number')}"


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
        proc = run_gh("pr", "view", pr_url, "--json", fields, timeout=REQUEST_TIMEOUT_SECONDS)
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


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """
    Parse a GitHub ISO-8601 timestamp (``...Z``).

    Returns:
        The parsed datetime, or None if absent/invalid.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fetch_copilot_events(pr_url: str) -> List[Dict[str, Any]]:
    """
    Fetch the PR timeline's Copilot work start/finish events, oldest first.

    Best-effort: any failure (missing gh, no access, bad output) yields an
    empty list so the caller falls back to the plain check status.

    Returns:
        The ``{event, created_at}`` dicts for Copilot work events.
    """
    match = _PR_URL_RE.search(pr_url)
    if not match:
        return []
    endpoint = f"repos/{match.group('owner')}/{match.group('repo')}/issues/{match.group('number')}/timeline"
    jq = '.[] | select(.event | startswith("copilot_work")) | {event, created_at}'
    try:
        proc = run_gh("api", endpoint, "--paginate", "--jq", jq, timeout=REQUEST_TIMEOUT_SECONDS)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    events = []
    for line in proc.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _count_runs_awaiting_approval(pr_url: str, head_sha: str) -> int:
    """
    Count workflow runs for ``head_sha`` that are blocked waiting for approval.

    These (e.g. first-time-contributor or Copilot-pushed runs) typically
    create no check runs, so they never appear in ``statusCheckRollup``.
    Best-effort: any failure yields 0.

    Returns:
        The number of runs awaiting approval.
    """
    match = _PR_URL_RE.search(pr_url)
    if not match:
        return 0
    endpoint = (
        f"repos/{match.group('owner')}/{match.group('repo')}/actions/runs"
        f"?head_sha={head_sha}&status=action_required&per_page=100"
    )
    try:
        proc = run_gh("api", endpoint, "--jq", ".total_count", timeout=REQUEST_TIMEOUT_SECONDS)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    if proc.returncode != 0:
        return 0
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return 0


def _count_copilot_review_runs_in_progress(pr_url: str, head_sha: str) -> int:
    """
    Count Copilot code review workflow runs for ``head_sha`` that haven't finished.

    The review runs as a separate dynamic workflow, so it never appears in
    ``statusCheckRollup``. Best-effort: any failure yields 0.

    Returns:
        The number of unfinished Copilot code review runs.
    """
    match = _PR_URL_RE.search(pr_url)
    if not match:
        return 0
    endpoint = (
        f"repos/{match.group('owner')}/{match.group('repo')}/actions/runs"
        f"?head_sha={head_sha}&per_page=100"
    )
    jq = '[.workflow_runs[] | select(.status != "completed" and (.name | test("copilot code review"; "i")))] | length'
    try:
        proc = run_gh("api", endpoint, "--jq", jq, timeout=REQUEST_TIMEOUT_SECONDS)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    if proc.returncode != 0:
        return 0
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return 0


def _latest_failure_time(rollup: List[Dict[str, Any]]) -> Optional[datetime]:
    """
    Find the latest completion time among failing checks.

    Returns:
        The latest failure time, or None if unknown.
    """
    times = []
    for check in rollup:
        resolved, passing = _check_outcome(check)
        if resolved and not passing:
            ts = _parse_ts(check.get("completedAt") or check.get("startedAt"))
            if ts:
                times.append(ts)
    return max(times) if times else None


def _active_copilot_session(
    events: List[Dict[str, Any]],
    failed_at: Optional[datetime],
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """
    Find an in-progress Copilot session that postdates the failure.

    A session is active when the latest Copilot event is a start (no later
    finish), it began after the failing checks completed (when known), and
    it is not older than ``COPILOT_SESSION_MAX_AGE``.

    Returns:
        The session start time, or None if no such session is active.
    """
    timed = []
    for event in events:
        ts = _parse_ts(event.get("created_at"))
        if ts:
            timed.append((ts, event.get("event")))
    if not timed:
        return None
    started_at, event_name = max(timed, key=lambda t: t[0])
    if event_name != _COPILOT_START_EVENT:
        return None
    if failed_at and started_at < failed_at:
        return None
    if (now or datetime.now(timezone.utc)) - started_at > COPILOT_SESSION_MAX_AGE:
        return None
    return started_at


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
        acknowledged_head_sha: Optional[str] = None,
        notes: str = "",
    ):
        """Initialize a GitHub PR watcher for the given PR URL."""
        self.pr_url = _normalize_pr_url(pr_url)
        super().__init__(watcher_id=watcher_id, label=label, notes=notes)
        # The head SHA we've last resolved a finished check-run state for.
        self.last_head_sha = last_head_sha
        # The head SHA the user has acknowledged (e.g. by clicking the row).
        # A resolved head SHA that doesn't match this is "unacknowledged" and
        # keeps notifying/highlighting across app restarts until acknowledged.
        self.acknowledged_head_sha = acknowledged_head_sha
        # In-memory only: whether this watcher has completed at least one
        # check() since the app started, used to re-alert once per restart -
        # and to notify on the very first check for a PR that was already
        # finished when it was added - for a completion that's still
        # unacknowledged.
        self._checked_since_start = False

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
        rollup = data.get("statusCheckRollup") or []
        status, detail = _status_from_checks(rollup)

        awaiting_approval = _count_runs_awaiting_approval(self.pr_url, head_sha) if head_sha else 0

        if awaiting_approval:
            # Workflow runs (e.g. from Copilot's push) are blocked until
            # someone approves them, so the user has to act - even if a
            # Copilot session is still running.
            status = Status.APPROVAL_REQUIRED
            detail = f"{awaiting_approval} workflow run(s) awaiting approval"
        elif status == Status.FAILURE and _active_copilot_session(
            _fetch_copilot_events(self.pr_url), _latest_failure_time(rollup)
        ):
            # Copilot is still working on it, so this isn't actionable yet.
            status, detail = Status.BUILDING, f"Copilot is working on a fix ({detail})"
        elif (
            status in (Status.SUCCESS, Status.BUILDING)
            and head_sha
            and _count_copilot_review_runs_in_progress(self.pr_url, head_sha)
        ):
            # Passing checks aren't "ready" while a Copilot review is pending.
            if status == Status.BUILDING:
                detail = f"{detail}, Copilot review in progress"
            else:
                status, detail = Status.BUILDING, f"Copilot review in progress ({detail})"

        # Merged PRs keep their check-derived status (a force-merge over
        # failing checks isn't green); we only annotate the detail.
        if (data.get("state") or "").upper() == "MERGED":
            detail = f"{detail} - merged"

        # Approval-required is tracked under its own key so that once the run
        # is approved, the resulting success/failure on the same commit still
        # notifies instead of looking like an already-seen state.
        state_key = None
        if head_sha is not None:
            state_key = f"{head_sha}#approval" if status == Status.APPROVAL_REQUIRED else head_sha

        unacknowledged = (
            status.is_actionable and state_key is not None and state_key != self.acknowledged_head_sha
        )
        # Notify when checks newly resolve on a new commit (head_sha
        # advanced), or - on the first poll since the app started - when a
        # completion from before the watcher was added is still
        # unacknowledged, so adding an already-finished PR still notifies
        # instead of silently requiring a new commit first.
        newly_actionable = unacknowledged and (
            state_key != self.last_head_sha or not self._checked_since_start
        )
        self._checked_since_start = True

        if state_key is not None and status != Status.BUILDING:
            # Only "commit" the head SHA once checks have finished, so we
            # don't miss a transition if we happen to poll mid-run.
            self.last_head_sha = state_key

        self.last_status = status
        self.last_detail = detail
        self.unacknowledged = unacknowledged
        return CheckResult(
            status=status, detail=detail, newly_actionable=newly_actionable, unacknowledged=unacknowledged
        )

    def acknowledge(self) -> None:
        """Mark the current resolved head SHA as seen, clearing the unacknowledged state."""
        self.acknowledged_head_sha = self.last_head_sha
        self.unacknowledged = False

    def forget_acknowledgment(self) -> None:
        """Clear the acknowledged head SHA so the last resolved commit re-notifies."""
        self.acknowledged_head_sha = None

    def to_config(self) -> Dict[str, Any]:
        """
        Serialize this watcher's URL, id, label, and head-SHA-tracking state.

        Returns:
            A dict suitable for JSON config storage.
        """
        return {
            "watcher_type": self.watcher_type,
            "id": self.id,
            "label": self.label,
            "pr_url": self.pr_url,
            "last_head_sha": self.last_head_sha,
            "acknowledged_head_sha": self.acknowledged_head_sha,
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
            acknowledged_head_sha=data.get("acknowledged_head_sha"),
            notes=data.get("notes", ""),
        )
