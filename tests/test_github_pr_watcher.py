"""
Unit tests for the GitHub PR watcher's state-transition / polling logic.

The ``gh`` CLI invocation is mocked by patching ``_run_gh_pr_view`` directly
so these tests run offline and fast, with no real ``gh`` auth required.
"""

from __future__ import annotations

from unittest.mock import patch

from watcher.core.base import Status
from watcher.watchers.github_pr import (
    GitHubPRWatcher,
    _default_label_from_url,
    _status_from_checks,
)

PR_URL = "https://github.com/INRIX/OSM-Map-Processing/pull/675"


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
