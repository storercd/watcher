"""
Unit tests for the Jenkins watcher's state-transition / polling logic.

HTTP calls are mocked by patching ``_fetch_job_json`` directly so these tests
run offline and fast.
"""

from __future__ import annotations

from unittest.mock import patch

from watcher.core.base import Status
from watcher.watchers.jenkins import JenkinsWatcher, _normalize_job_url, _status_from_job_json


def _job_json(color: str, build_number: int = None) -> dict:
    data = {"color": color}
    if build_number is not None:
        data["lastBuild"] = {"number": build_number}
    else:
        data["lastBuild"] = None
    return data


class TestStatusFromJobJson:
    """Tests for translating Jenkins job colors into normalized Status values."""

    def test_blue_is_success(self):
        """A blue job color means the last build succeeded."""
        status, _detail, _num = _status_from_job_json(_job_json("blue", 5))
        assert status == Status.SUCCESS

    def test_green_is_success(self):
        """A green job color also means the last build succeeded."""
        status, _detail, _num = _status_from_job_json(_job_json("green", 5))
        assert status == Status.SUCCESS

    def test_red_is_failure(self):
        """A red job color means the last build failed."""
        status, _detail, _num = _status_from_job_json(_job_json("red", 5))
        assert status == Status.FAILURE

    def test_yellow_is_failure(self):
        """A yellow (unstable) job color is treated as a failure."""
        status, _detail, _num = _status_from_job_json(_job_json("yellow", 5))
        assert status == Status.FAILURE

    def test_anime_suffix_is_building(self):
        """A "_anime" color suffix means a build is currently running."""
        status, _detail, _num = _status_from_job_json(_job_json("blue_anime", 6))
        assert status == Status.BUILDING

    def test_notbuilt_is_idle(self):
        """A job that has never been built reports idle status."""
        status, _detail, _num = _status_from_job_json(_job_json("notbuilt"))
        assert status == Status.IDLE

    def test_disabled_is_idle(self):
        """A disabled job reports idle status."""
        status, _detail, _num = _status_from_job_json(_job_json("disabled"))
        assert status == Status.IDLE

    def test_aborted_is_failure(self):
        """An aborted build is treated as a failure so it's flagged for attention."""
        status, _detail, _num = _status_from_job_json(_job_json("aborted", 5))
        assert status == Status.FAILURE

    def test_unknown_color(self):
        """An unrecognized job color falls back to unknown status."""
        status, _detail, _num = _status_from_job_json(_job_json("mystery-color"))
        assert status == Status.UNKNOWN


class TestNormalizeJobUrl:
    """Tests for ``_normalize_job_url``'s build-number stripping."""

    def test_strips_trailing_build_number(self):
        """A trailing build number is stripped from the job URL."""
        assert (
            _normalize_job_url("http://jenkins.example.com/job/my-job/645")
            == "http://jenkins.example.com/job/my-job"
        )

    def test_strips_trailing_build_number_with_trailing_slash(self):
        """A trailing build number is stripped even with a trailing slash."""
        assert (
            _normalize_job_url("http://jenkins.example.com/job/my-job/645/")
            == "http://jenkins.example.com/job/my-job"
        )

    def test_leaves_job_level_url_unchanged(self):
        """A URL that already points at the job itself is left unchanged."""
        assert (
            _normalize_job_url("http://jenkins.example.com/job/my-job")
            == "http://jenkins.example.com/job/my-job"
        )

    def test_does_not_strip_numeric_job_name_segment(self):
        """A numeric job name is not mistaken for a trailing build number."""
        # A job name that is itself numeric (e.g. "job/2024") should survive,
        # since we can't tell it apart from a build number by string alone;
        # this mirrors Jenkins' own ambiguity here and is an accepted edge case.
        assert (
            _normalize_job_url("http://jenkins.example.com/job/2024")
            == "http://jenkins.example.com/job/2024"
        )


class TestJenkinsWatcherCheck:
    """Tests for JenkinsWatcher.check() polling and state-transition logic."""

    def _make_watcher(self, job_url="https://jenkins.example.com/job/my-job"):
        """
        Build a JenkinsWatcher pointed at a test job URL.

        Returns:
            A JenkinsWatcher configured with the given job URL.
        """
        return JenkinsWatcher(job_url=job_url)

    @patch("watcher.watchers.jenkins._fetch_job_json")
    def test_first_check_notifies_when_unacknowledged(self, mock_fetch):
        """
        The first check notifies if the completed build is unacknowledged.

        This matters when the app restarts: a previously-completed build the
        user never clicked on should alert again, not go silent forever.
        """
        mock_fetch.return_value = _job_json("blue", 10)
        watcher = self._make_watcher()

        result = watcher.check()

        assert result.status == Status.SUCCESS
        assert result.newly_actionable is True
        assert result.unacknowledged is True
        assert watcher.last_build_number == 10

    @patch("watcher.watchers.jenkins._fetch_job_json")
    def test_same_build_number_does_not_renotify_within_a_run(self, mock_fetch):
        """Polling again with the same build number does not re-notify."""
        mock_fetch.return_value = _job_json("blue", 10)
        watcher = self._make_watcher()
        watcher.check()  # first check establishes baseline and notifies

        result = watcher.check()  # second poll, same build, same run

        assert result.newly_actionable is False
        assert result.unacknowledged is True  # still pending acknowledgment

    @patch("watcher.watchers.jenkins._fetch_job_json")
    def test_restart_renotifies_if_still_unacknowledged(self, mock_fetch):
        """A simulated app restart re-notifies for a completion never acknowledged."""
        mock_fetch.return_value = _job_json("blue", 10)
        watcher = self._make_watcher()
        watcher.check()

        restarted = JenkinsWatcher.from_config(watcher.to_config())
        result = restarted.check()

        assert result.newly_actionable is True
        assert result.unacknowledged is True

    @patch("watcher.watchers.jenkins._fetch_job_json")
    def test_restart_does_not_renotify_once_acknowledged(self, mock_fetch):
        """Acknowledging a completion stops it from re-notifying after a restart."""
        mock_fetch.return_value = _job_json("blue", 10)
        watcher = self._make_watcher()
        watcher.check()
        watcher.acknowledge()

        restarted = JenkinsWatcher.from_config(watcher.to_config())
        result = restarted.check()

        assert result.newly_actionable is False
        assert result.unacknowledged is False

    @patch("watcher.watchers.jenkins._fetch_job_json")
    def test_new_completed_build_notifies(self, mock_fetch):
        """A newly completed build after the baseline triggers a notification."""
        watcher = self._make_watcher()
        mock_fetch.return_value = _job_json("blue", 10)
        watcher.check()  # baseline at build 10

        mock_fetch.return_value = _job_json("red", 11)
        result = watcher.check()  # build 11 finished as a failure

        assert result.status == Status.FAILURE
        assert result.newly_actionable is True
        assert watcher.last_build_number == 11

    @patch("watcher.watchers.jenkins._fetch_job_json")
    def test_building_state_does_not_notify_or_advance_build_number(self, mock_fetch):
        """A build in progress does not notify or advance the tracked build number."""
        watcher = self._make_watcher()
        mock_fetch.return_value = _job_json("blue", 10)
        watcher.check()  # baseline at build 10

        mock_fetch.return_value = _job_json("blue_anime", 11)
        result = watcher.check()  # build 11 is in progress

        assert result.status == Status.BUILDING
        assert result.newly_actionable is False
        # last_build_number should not advance until the build finishes
        assert watcher.last_build_number == 10

        mock_fetch.return_value = _job_json("blue", 11)
        result = watcher.check()  # build 11 finishes successfully

        assert result.status == Status.SUCCESS
        assert result.newly_actionable is True
        assert watcher.last_build_number == 11

    @patch("watcher.watchers.jenkins._fetch_job_json")
    def test_fetch_error_returns_error_status(self, mock_fetch):
        """A fetch failure is reported as an error status with the error detail."""
        mock_fetch.side_effect = ValueError("boom")
        watcher = self._make_watcher()

        result = watcher.check()

        assert result.status == Status.ERROR
        assert "boom" in result.detail

    def test_config_round_trip(self):
        """A watcher serialized via to_config() can be restored via from_config()."""
        watcher = JenkinsWatcher(job_url="https://jenkins.example.com/job/my-job", label="My Job")
        watcher.last_build_number = 42
        watcher.acknowledged_build_number = 41

        config = watcher.to_config()
        restored = JenkinsWatcher.from_config(config)

        assert restored.job_url == watcher.job_url
        assert restored.label == watcher.label
        assert restored.last_build_number == 42
        assert restored.acknowledged_build_number == 41
        assert restored.id == watcher.id

    @patch("watcher.watchers.jenkins._fetch_job_json")
    def test_acknowledge_clears_unacknowledged_state(self, mock_fetch):
        """Calling acknowledge() clears the watcher's unacknowledged flag."""
        mock_fetch.return_value = _job_json("blue", 10)
        watcher = self._make_watcher()
        watcher.check()
        assert watcher.unacknowledged is True

        watcher.acknowledge()

        assert watcher.unacknowledged is False
        assert watcher.acknowledged_build_number == 10

    @patch("watcher.watchers.jenkins._fetch_job_json")
    def test_forget_acknowledgment_makes_restart_notify_again(self, mock_fetch):
        """
        forget_acknowledgment() makes an already-acknowledged completion notify again.

        Simulates an app restart (reconstructing via to_config()/from_config(),
        since _checked_since_start is in-memory only) with the "re-notify on
        restart" setting applying forget_acknowledgment() to the restored watcher.
        """
        mock_fetch.return_value = _job_json("blue", 10)
        watcher = self._make_watcher()
        watcher.check()
        watcher.acknowledge()
        assert watcher.acknowledged_build_number == 10

        restarted = JenkinsWatcher.from_config(watcher.to_config())
        restarted.forget_acknowledgment()
        assert restarted.acknowledged_build_number is None

        result = restarted.check()

        assert result.newly_actionable is True
        assert result.unacknowledged is True

    def test_default_label_from_url(self):
        """The default label is derived from the job URL's trailing path segment."""
        watcher = JenkinsWatcher(job_url="https://jenkins.example.com/job/my-cool-job")
        assert watcher.label == "my-cool-job"

    def test_build_specific_url_is_normalized_to_job_url(self):
        """A build-specific URL is normalized to the job-level URL on construction."""
        # Regression test: pasting the URL of a specific build (e.g. copied
        # from the browser while viewing that build) must not stick around,
        # since per-build JSON has no "color"/"lastBuild" and would silently
        # poll as Status.UNKNOWN forever.
        watcher = JenkinsWatcher(job_url="https://jenkins.example.com/job/my-cool-job/645")
        assert watcher.job_url == "https://jenkins.example.com/job/my-cool-job"
        assert watcher.label == "my-cool-job"


def test_normalize_strips_console_and_other_subpages():
    base = "http://j.example/job/MAPS/job/MapFlow/job/map_update"
    for suffix in ("/357/console", "/357/parameters/", "/lastBuild/consoleText", "/configure", "/357/console?x=1"):
        assert _normalize_job_url(base + suffix) == base
