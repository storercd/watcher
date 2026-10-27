"""
Unit tests for the GitHub PR watcher's state-transition / polling logic.

The ``gh`` CLI invocation is mocked by patching ``_run_gh_pr_view`` directly
so these tests run offline and fast, with no real ``gh`` auth required.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from watcher.core.base import Status
from watcher.watchers.github_pr import (
    GitHubPRWatcher,
    _active_copilot_session,
    _default_label_from_url,
    _status_from_checks,
)

PR_URL = "https://github.com/INRIX/OSM-Map-Processing/pull/675"


@pytest.fixture(autouse=True)
def _no_copilot_events():
    """Keep tests offline: by default the PR timeline has no Copilot events."""
    with patch("watcher.watchers.github_pr._fetch_copilot_events", return_value=[]), patch(
        "watcher.watchers.github_pr._count_runs_awaiting_approval", return_value=0
    ), patch("watcher.watchers.github_pr._count_copilot_review_runs_in_progress", return_value=0):
        yield


def _check_run(conclusion, status="COMPLETED"):
    return {"status": status, "conclusion": conclusion}


def _status_context(state):
    return {"state": state}


def _pr_data(head_sha, rollup):
    return {
        "headRefOid": head_sha,
        "number": 675,
        "title": "Some PR",
        "state": "OPEN",
        "statusCheckRollup": rollup,
    }


class TestStatusFromChecks:
    """Tests for translating a statusCheckRollup into a normalized Status."""

    def test_empty_rollup_is_idle(self):
        """No status checks at all is reported as idle."""
        status, _detail = _status_from_checks([])
        assert status == Status.IDLE

    def test_all_passing_check_runs_is_success(self):
        """All CheckRun entries completed successfully means success."""
        status, _detail = _status_from_checks([_check_run("SUCCESS"), _check_run("NEUTRAL")])
        assert status == Status.SUCCESS

    def test_any_failing_check_run_is_failure(self):
        """Any failing CheckRun conclusion means overall failure."""
        status, _detail = _status_from_checks([_check_run("SUCCESS"), _check_run("FAILURE")])
        assert status == Status.FAILURE

    def test_in_progress_check_run_is_building(self):
        """A CheckRun that hasn't completed yet means checks are still running."""
        status, _detail = _status_from_checks([_check_run("SUCCESS"), _check_run(None, status="IN_PROGRESS")])
        assert status == Status.BUILDING

    def test_legacy_status_context_pending_is_building(self):
        """A legacy commit StatusContext in PENDING state means still running."""
        status, _detail = _status_from_checks([_status_context("PENDING")])
        assert status == Status.BUILDING

    def test_legacy_status_context_error_is_failure(self):
        """A legacy commit StatusContext in ERROR state means failure."""
        status, _detail = _status_from_checks([_status_context("SUCCESS"), _status_context("ERROR")])
        assert status == Status.FAILURE

    def test_cancelled_check_run_is_failure(self):
        """A cancelled CheckRun conclusion counts as a failure."""
        status, _detail = _status_from_checks([_check_run("CANCELLED")])
        assert status == Status.FAILURE


class TestDefaultLabelFromUrl:
    """Tests for deriving a short label from a GitHub PR URL."""

    def test_derives_owner_repo_number(self):
        """A standard PR URL is shortened to owner/repo#number."""
        assert _default_label_from_url(PR_URL) == "INRIX/OSM-Map-Processing#675"

    def test_falls_back_to_raw_url(self):
        """A URL that doesn't match the expected pattern is returned as-is."""
        assert _default_label_from_url("not-a-pr-url") == "not-a-pr-url"


class TestGitHubPRWatcherCheck:
    """Tests for GitHubPRWatcher.check() polling and state-transition logic."""

    def _make_watcher(self, pr_url=PR_URL):
        """
        Build a GitHubPRWatcher pointed at a test PR URL.

        Returns:
            A GitHubPRWatcher configured with the given PR URL.
        """
        return GitHubPRWatcher(pr_url=pr_url)

    @patch("watcher.watchers.github_pr._run_gh_pr_view")
    def test_first_check_notifies_for_already_finished_pr(self, mock_run):
        """
        The very first check still notifies if the PR was already resolved.

        A PR can already have finished checks by the time it's added (e.g. it
        completed moments before the user pasted the URL), so the first poll
        must notify instead of silently requiring a new commit first.
        """
        mock_run.return_value = _pr_data("sha1", [_check_run("SUCCESS")])
        watcher = self._make_watcher()

        result = watcher.check()

        assert result.status == Status.SUCCESS
        assert result.newly_actionable is True
        assert result.unacknowledged is True
        assert watcher.last_head_sha == "sha1"

    @patch("watcher.watchers.github_pr._run_gh_pr_view")
    def test_same_head_sha_does_not_renotify(self, mock_run):
        """Polling again with the same head commit does not re-notify."""
        mock_run.return_value = _pr_data("sha1", [_check_run("SUCCESS")])
        watcher = self._make_watcher()
        watcher.check()  # first check establishes baseline

        result = watcher.check()  # second poll, same commit

        assert result.newly_actionable is False

    @patch("watcher.watchers.github_pr._run_gh_pr_view")
    def test_forget_acknowledgment_makes_restart_notify_again(self, mock_run):
        """
        forget_acknowledgment() makes an already-acknowledged completion notify again.

        Simulates an app restart (reconstructing via to_config()/from_config(),
        since _checked_since_start is in-memory only) with the "re-notify on
        restart" setting applying forget_acknowledgment() to the restored watcher.
        """
        mock_run.return_value = _pr_data("sha1", [_check_run("SUCCESS")])
        watcher = self._make_watcher()
        watcher.check()
        watcher.acknowledge()
        assert watcher.acknowledged_head_sha == "sha1"

        restarted = GitHubPRWatcher.from_config(watcher.to_config())
        restarted.forget_acknowledgment()
        assert restarted.acknowledged_head_sha is None

        result = restarted.check()

        assert result.newly_actionable is True
        assert result.unacknowledged is True

    @patch("watcher.watchers.github_pr._run_gh_pr_view")
    def test_new_completed_commit_notifies(self, mock_run):
        """A newly completed check run after the baseline triggers a notification."""
        watcher = self._make_watcher()
        mock_run.return_value = _pr_data("sha1", [_check_run("SUCCESS")])
        watcher.check()  # baseline at sha1

        mock_run.return_value = _pr_data("sha2", [_check_run("FAILURE")])
        result = watcher.check()  # sha2 finished as a failure

        assert result.status == Status.FAILURE
        assert result.newly_actionable is True
        assert watcher.last_head_sha == "sha2"

    @patch("watcher.watchers.github_pr._run_gh_pr_view")
    def test_building_state_does_not_notify_or_advance_head_sha(self, mock_run):
        """Checks still running do not notify or advance the tracked head SHA."""
        watcher = self._make_watcher()
        mock_run.return_value = _pr_data("sha1", [_check_run("SUCCESS")])
        watcher.check()  # baseline at sha1

        mock_run.return_value = _pr_data("sha2", [_check_run(None, status="IN_PROGRESS")])
        result = watcher.check()  # sha2 is still running

        assert result.status == Status.BUILDING
        assert result.newly_actionable is False
        assert watcher.last_head_sha == "sha1"

        mock_run.return_value = _pr_data("sha2", [_check_run("SUCCESS")])
        result = watcher.check()  # sha2 finishes successfully

        assert result.status == Status.SUCCESS
        assert result.newly_actionable is True
        assert watcher.last_head_sha == "sha2"

    @patch("watcher.watchers.github_pr._run_gh_pr_view")
    def test_gh_error_returns_error_status(self, mock_run):
        """A gh CLI failure is reported as an error status with the error detail."""
        mock_run.side_effect = RuntimeError("gh: not authenticated")
        watcher = self._make_watcher()

        result = watcher.check()

        assert result.status == Status.ERROR
        assert "not authenticated" in result.detail

    def test_config_round_trip(self):
        """A watcher serialized via to_config() can be restored via from_config()."""
        watcher = GitHubPRWatcher(pr_url=PR_URL, label="My PR")
        watcher.last_head_sha = "deadbeef"

        config = watcher.to_config()
        restored = GitHubPRWatcher.from_config(config)

        assert restored.pr_url == watcher.pr_url
        assert restored.label == watcher.label
        assert restored.last_head_sha == "deadbeef"
        assert restored.id == watcher.id

    def test_default_label_from_url(self):
        """The default label is derived as owner/repo#number."""
        watcher = GitHubPRWatcher(pr_url=PR_URL)
        assert watcher.label == "INRIX/OSM-Map-Processing#675"


NOW = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)


def _ev(event, minutes_ago):
    return {"event": event, "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat()}


class TestActiveCopilotSession:
    """Tests for detecting an in-progress Copilot session."""

    def test_started_after_failure_is_active(self):
        """An unfinished session that began after the failure is active."""
        failed_at = NOW - timedelta(minutes=30)
        assert _active_copilot_session([_ev("copilot_work_started", 5)], failed_at, NOW)

    def test_started_before_failure_is_not_active(self):
        """A session that predates the failure can't be fixing it."""
        failed_at = NOW - timedelta(minutes=5)
        assert not _active_copilot_session([_ev("copilot_work_started", 30)], failed_at, NOW)

    def test_finished_session_is_not_active(self):
        """A finish event after the start ends the session."""
        events = [_ev("copilot_work_started", 20), _ev("copilot_work_finished", 10)]
        assert not _active_copilot_session(events, None, NOW)

    def test_stale_session_is_not_active(self):
        """A session with no finish event for hours is treated as stalled."""
        assert not _active_copilot_session([_ev("copilot_work_started", 600)], None, NOW)


class TestCopilotOverridesFailure:
    """Tests for the watcher reporting building while Copilot fixes a failure."""

    @patch("watcher.watchers.github_pr._active_copilot_session", return_value=NOW)
    @patch("watcher.watchers.github_pr._run_gh_pr_view")
    def test_failure_with_active_copilot_is_building(self, mock_run, _active):
        """A failing PR with active Copilot work reports building and is not actionable."""
        mock_run.return_value = _pr_data("aaa", [_check_run("FAILURE")])
        result = GitHubPRWatcher(pr_url=PR_URL).check()
        assert result.status == Status.BUILDING
        assert not result.newly_actionable
        assert "Copilot" in result.detail

    @patch("watcher.watchers.github_pr._run_gh_pr_view")
    def test_failure_without_copilot_stays_failure(self, mock_run):
        """Without active Copilot work, a failing PR still reports failure."""
        mock_run.return_value = _pr_data("aaa", [_check_run("FAILURE")])
        assert GitHubPRWatcher(pr_url=PR_URL).check().status == Status.FAILURE


class TestCopilotReviewHoldsSuccess:
    """Passing checks are not ready while a Copilot review is running."""

    @patch("watcher.watchers.github_pr._count_copilot_review_runs_in_progress", return_value=1)
    @patch("watcher.watchers.github_pr._run_gh_pr_view")
    def test_success_with_review_running_is_building(self, mock_run, _review):
        mock_run.return_value = _pr_data("aaa", [_check_run("SUCCESS")])
        result = GitHubPRWatcher(pr_url=PR_URL).check()
        assert result.status == Status.BUILDING
        assert not result.newly_actionable
        assert "review" in result.detail

    @patch("watcher.watchers.github_pr._count_copilot_review_runs_in_progress", return_value=1)
    @patch("watcher.watchers.github_pr._run_gh_pr_view")
    def test_running_checks_mention_review(self, mock_run, _review):
        mock_run.return_value = _pr_data("aaa", [_check_run("SUCCESS"), _check_run(None, "IN_PROGRESS")])
        result = GitHubPRWatcher(pr_url=PR_URL).check()
        assert result.status == Status.BUILDING
        assert "checks running" in result.detail
        assert "Copilot review" in result.detail

    @patch("watcher.watchers.github_pr._count_copilot_review_runs_in_progress")
    @patch("watcher.watchers.github_pr._run_gh_pr_view")
    def test_success_notifies_once_review_finishes(self, mock_run, mock_review):
        mock_run.return_value = _pr_data("aaa", [_check_run("SUCCESS")])
        mock_review.return_value = 1
        watcher = GitHubPRWatcher(pr_url=PR_URL)
        watcher.check()
        mock_review.return_value = 0
        result = watcher.check()
        assert result.status == Status.SUCCESS
        assert result.newly_actionable


class TestApprovalRequired:
    """Tests for the approval-required (yellow) state."""

    @patch("watcher.watchers.github_pr._count_runs_awaiting_approval", return_value=2)
    @patch("watcher.watchers.github_pr._run_gh_pr_view")
    def test_awaiting_approval_notifies(self, mock_run, _count):
        """Runs awaiting approval report approval_required and notify."""
        mock_run.return_value = _pr_data("aaa", [])
        result = GitHubPRWatcher(pr_url=PR_URL).check()
        assert result.status == Status.APPROVAL_REQUIRED
        assert result.newly_actionable
        assert result.unacknowledged

    @patch("watcher.watchers.github_pr._active_copilot_session", return_value=NOW)
    @patch("watcher.watchers.github_pr._count_runs_awaiting_approval", return_value=1)
    @patch("watcher.watchers.github_pr._run_gh_pr_view")
    def test_awaiting_approval_wins_over_active_copilot(self, mock_run, _count, _active):
        """Pending approval needs the user, so it outranks an active Copilot session."""
        mock_run.return_value = _pr_data("aaa", [])
        result = GitHubPRWatcher(pr_url=PR_URL).check()
        assert result.status == Status.APPROVAL_REQUIRED
        assert result.newly_actionable

    @patch("watcher.watchers.github_pr._count_runs_awaiting_approval")
    @patch("watcher.watchers.github_pr._run_gh_pr_view")
    def test_result_after_approval_on_same_commit_notifies(self, mock_run, mock_count):
        """After approving, the success on the same commit still notifies."""
        mock_run.return_value = _pr_data("aaa", [])
        mock_count.return_value = 1
        watcher = GitHubPRWatcher(pr_url=PR_URL)
        watcher.check()
        watcher.acknowledge()

        mock_count.return_value = 0
        mock_run.return_value = _pr_data("aaa", [_check_run("SUCCESS")])
        result = watcher.check()
        assert result.status == Status.SUCCESS
        assert result.newly_actionable
