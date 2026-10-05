"""Unit tests for the Jenkins watcher's state-transition / polling logic.

HTTP calls are mocked by patching ``_fetch_job_json`` directly so these tests
run offline and fast.
"""

from __future__ import annotations

from unittest.mock import patch

from watcher.core.base import Status
from watcher.watchers.jenkins import JenkinsWatcher, _status_from_job_json


def _job_json(color: str, build_number: int = None) -> dict:
    data = {"color": color}
    if build_number is not None:
        data["lastBuild"] = {"number": build_number}
    else:
        data["lastBuild"] = None
    return data


class TestStatusFromJobJson:
    def test_blue_is_success(self):
        status, _detail, _num = _status_from_job_json(_job_json("blue", 5))
        assert status == Status.SUCCESS

    def test_green_is_success(self):
        status, _detail, _num = _status_from_job_json(_job_json("green", 5))
        assert status == Status.SUCCESS

    def test_red_is_failure(self):
        status, _detail, _num = _status_from_job_json(_job_json("red", 5))
        assert status == Status.FAILURE

    def test_yellow_is_failure(self):
        status, _detail, _num = _status_from_job_json(_job_json("yellow", 5))
        assert status == Status.FAILURE

    def test_anime_suffix_is_building(self):
        status, _detail, _num = _status_from_job_json(_job_json("blue_anime", 6))
        assert status == Status.BUILDING

    def test_notbuilt_is_idle(self):
        status, _detail, _num = _status_from_job_json(_job_json("notbuilt"))
        assert status == Status.IDLE

    def test_disabled_is_idle(self):
        status, _detail, _num = _status_from_job_json(_job_json("disabled"))
        assert status == Status.IDLE

    def test_unknown_color(self):
        status, _detail, _num = _status_from_job_json(_job_json("aborted_anime".replace("_anime", "")))
        assert status == Status.UNKNOWN


class TestJenkinsWatcherCheck:
    def _make_watcher(self, job_url="https://jenkins.example.com/job/my-job"):
        return JenkinsWatcher(job_url=job_url)

    @patch("watcher.watchers.jenkins._fetch_job_json")
    def test_first_check_never_notifies(self, mock_fetch):
        mock_fetch.return_value = _job_json("blue", 10)
        watcher = self._make_watcher()

        result = watcher.check()

        assert result.status == Status.SUCCESS
        assert result.newly_actionable is False
        assert watcher.last_build_number == 10

    @patch("watcher.watchers.jenkins._fetch_job_json")
    def test_same_build_number_does_not_renotify(self, mock_fetch):
        mock_fetch.return_value = _job_json("blue", 10)
        watcher = self._make_watcher()
        watcher.check()  # first check establishes baseline

        result = watcher.check()  # second poll, same build

        assert result.newly_actionable is False

    @patch("watcher.watchers.jenkins._fetch_job_json")
    def test_new_completed_build_notifies(self, mock_fetch):
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
        mock_fetch.side_effect = ValueError("boom")
        watcher = self._make_watcher()

        result = watcher.check()

        assert result.status == Status.ERROR
        assert "boom" in result.detail

    def test_config_round_trip(self):
        watcher = JenkinsWatcher(job_url="https://jenkins.example.com/job/my-job", label="My Job")
        watcher.last_build_number = 42

        config = watcher.to_config()
        restored = JenkinsWatcher.from_config(config)

        assert restored.job_url == watcher.job_url
        assert restored.label == watcher.label
        assert restored.last_build_number == 42
        assert restored.id == watcher.id

    def test_default_label_from_url(self):
        watcher = JenkinsWatcher(job_url="https://jenkins.example.com/job/my-cool-job")
        assert watcher.label == "my-cool-job"
