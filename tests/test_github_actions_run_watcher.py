"""
Unit tests for the GitHub Actions run watcher's state-transition / polling logic.

The ``gh`` CLI invocation is mocked by patching ``_run_gh_run_view`` directly
so these tests run offline and fast, with no real ``gh`` auth required.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from watcher.core.base import Status
from watcher.watchers.github_actions_run import (
    GitHubActionsRunWatcher,
    _default_label_from_url,
    _parse_run_url,
    _status_from_run,
)

RUN_URL = "https://github.com/INRIX/OSM-Map-Processing/actions/runs/37353299420"


def _run_data(status, conclusion=None, attempt=1):
    return {"status": status, "conclusion": conclusion, "attempt": attempt}


class TestParseRunUrl:
    """Tests for extracting (owner, repo, run_id) from a run URL."""

    def test_parses_owner_repo_run_id(self):
        """A standard run URL yields owner, repo, and run_id."""
        assert _parse_run_url(RUN_URL) == ("INRIX", "OSM-Map-Processing", "37353299420")

    def test_raises_on_non_run_url(self):
        """A URL that doesn't match the expected pattern raises ValueError."""
        with pytest.raises(ValueError):
            _parse_run_url("not-a-run-url")


class TestStatusFromRun:
    """Tests for translating `gh run view` JSON into a normalized Status."""

    def test_queued_is_building(self):
        """A queued run is reported as building."""
        status, _detail, _attempt = _status_from_run(_run_data("queued"))
        assert status == Status.BUILDING

    def test_in_progress_is_building(self):
        """An in-progress run is reported as building."""
        status, _detail, _attempt = _status_from_run(_run_data("in_progress"))
        assert status == Status.BUILDING

    def test_completed_success_is_success(self):
        """A completed run with a success conclusion is reported as success."""
        status, _detail, _attempt = _status_from_run(_run_data("completed", "success"))
        assert status == Status.SUCCESS

    def test_completed_failure_is_failure(self):
        """A completed run with a failure conclusion is reported as failure."""
        status, _detail, _attempt = _status_from_run(_run_data("completed", "failure"))
        assert status == Status.FAILURE

    def test_completed_cancelled_is_failure(self):
        """A completed run with a cancelled conclusion is reported as failure."""
        status, _detail, _attempt = _status_from_run(_run_data("completed", "cancelled"))
        assert status == Status.FAILURE

    def test_completed_neutral_is_success(self):
        """A completed run with a neutral conclusion is not treated as a failure."""
        status, _detail, _attempt = _status_from_run(_run_data("completed", "neutral"))
        assert status == Status.SUCCESS

    def test_attempt_is_passed_through(self):
        """The run's attempt number is returned unchanged."""
        _status, _detail, attempt = _status_from_run(_run_data("completed", "success", attempt=3))
        assert attempt == 3


class TestDefaultLabelFromUrl:
    """Tests for deriving a short label from a GitHub Actions run URL."""

    def test_derives_owner_repo_run_id(self):
        """A standard run URL is shortened to owner/repo run #id."""
        assert _default_label_from_url(RUN_URL) == "INRIX/OSM-Map-Processing run #37353299420"

    def test_falls_back_to_raw_url(self):
        """A URL that doesn't match the expected pattern is returned as-is."""
        assert _default_label_from_url("not-a-run-url") == "not-a-run-url"


class TestGitHubActionsRunWatcherCheck:
    """Tests for GitHubActionsRunWatcher.check() polling and state-transition logic."""

    def _make_watcher(self, run_url=RUN_URL):
        """
        Build a GitHubActionsRunWatcher pointed at a test run URL.

        Returns:
            A GitHubActionsRunWatcher configured with the given run URL.
        """
        return GitHubActionsRunWatcher(run_url=run_url)

    def test_rejects_non_run_url(self):
        """Constructing a watcher with a non-run URL raises ValueError."""
        with pytest.raises(ValueError):
            self._make_watcher(run_url="not-a-run-url")

    @patch("watcher.watchers.github_actions_run._run_gh_run_view")
    def test_first_check_of_completed_run_notifies(self, mock_run):
        """Adding a watcher for an already-finished run notifies right away."""
        mock_run.return_value = _run_data("completed", "success", attempt=1)
        watcher = self._make_watcher()

        result = watcher.check()

        assert result.status == Status.SUCCESS
        assert result.newly_actionable is True
        assert result.unacknowledged is True
        assert watcher.last_attempt == 1

    @patch("watcher.watchers.github_actions_run._run_gh_run_view")
    def test_still_building_does_not_notify_or_advance_attempt(self, mock_run):
        """A run still in progress does not notify or advance the tracked attempt."""
        mock_run.return_value = _run_data("in_progress")
        watcher = self._make_watcher()

        result = watcher.check()

        assert result.status == Status.BUILDING
        assert result.newly_actionable is False
        assert watcher.last_attempt is None

    @patch("watcher.watchers.github_actions_run._run_gh_run_view")
    def test_acknowledged_completion_does_not_renotify(self, mock_run):
        """Polling again after acknowledging the same attempt does not re-notify."""
        mock_run.return_value = _run_data("completed", "success", attempt=1)
        watcher = self._make_watcher()
        watcher.check()
        watcher.acknowledge()

        result = watcher.check()

        assert result.newly_actionable is False
        assert result.unacknowledged is False

    @patch("watcher.watchers.github_actions_run._run_gh_run_view")
    def test_new_attempt_after_rerun_notifies(self, mock_run):
        """A re-run that completes with a new attempt number notifies again."""
        mock_run.return_value = _run_data("completed", "failure", attempt=1)
        watcher = self._make_watcher()
        watcher.check()
        watcher.acknowledge()

        mock_run.return_value = _run_data("completed", "success", attempt=2)
        result = watcher.check()

        assert result.status == Status.SUCCESS
        assert result.newly_actionable is True
        assert watcher.last_attempt == 2

    @patch("watcher.watchers.github_actions_run._run_gh_run_view")
    def test_unacknowledged_completion_renotifies_after_restart(self, mock_run):
        """A still-unacknowledged completion re-notifies on the first check after restart."""
        mock_run.return_value = _run_data("completed", "failure", attempt=1)
        watcher = self._make_watcher()
        watcher.check()  # first check of this "run" never notifies

        # Simulate an app restart: a fresh watcher instance restored from
        # config, with the same unacknowledged attempt.
        restored = GitHubActionsRunWatcher.from_config(watcher.to_config())
        result = restored.check()

        assert result.newly_actionable is True

    @patch("watcher.watchers.github_actions_run._run_gh_run_view")
    def test_gh_error_returns_error_status(self, mock_run):
        """A gh CLI failure is reported as an error status with the error detail."""
        mock_run.side_effect = RuntimeError("gh: not authenticated")
        watcher = self._make_watcher()

        result = watcher.check()

        assert result.status == Status.ERROR
        assert "not authenticated" in result.detail

    def test_config_round_trip(self):
        """A watcher serialized via to_config() can be restored via from_config()."""
        watcher = GitHubActionsRunWatcher(run_url=RUN_URL, label="My Run")
        watcher.last_attempt = 2
        watcher.acknowledged_attempt = 1

        config = watcher.to_config()
        restored = GitHubActionsRunWatcher.from_config(config)

        assert restored.run_url == watcher.run_url
        assert restored.label == watcher.label
        assert restored.last_attempt == 2
        assert restored.acknowledged_attempt == 1
        assert restored.id == watcher.id

    def test_default_label_from_url(self):
        """The default label is derived as owner/repo run #id."""
        watcher = GitHubActionsRunWatcher(run_url=RUN_URL)
        assert watcher.label == "INRIX/OSM-Map-Processing run #37353299420"
