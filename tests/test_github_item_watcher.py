"""Tests for the long-term GitHub PR/issue watcher and per-watcher polling."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from watcher.core.base import CheckResult, Status
from watcher.core.engine import Engine
from watcher.core.scheduler import Scheduler
from watcher.watchers.github_item import GitHubItemWatcher, triggers_for_url

PR_URL = "https://github.com/acme/web/pull/7"
ISSUE_URL = "https://github.com/acme/api/issues/310"


def _view(state="OPEN", updated="2026-01-01T00:00:00Z"):
    return {"state": state, "updatedAt": updated, "title": "t"}


def _check(watcher, data):
    with patch("watcher.watchers.github_item._run_gh_view", return_value=data):
        return watcher.check()


class TestTriggers:
    """Which triggers are valid for which URL."""

    def test_pr_supports_checks(self):
        """PRs offer all three triggers."""
        assert triggers_for_url(PR_URL) == ("closed", "activity", "checks")

    def test_issue_has_no_checks(self):
        """Issues don't have status checks."""
        assert triggers_for_url(ISSUE_URL) == ("closed", "activity")

    def test_invalid_trigger_rejected(self):
        """A trigger invalid for the URL raises ValueError."""
        with pytest.raises(ValueError):
            GitHubItemWatcher(ISSUE_URL, trigger="checks")

    def test_not_auto_detected(self):
        """Long-term watchers are never picked by URL auto-detection."""
        assert GitHubItemWatcher.matches_url(PR_URL) is False


class TestClosedTrigger:
    """The merged/closed trigger."""

    def test_open_is_idle(self):
        """An open PR stays idle and silent."""
        result = _check(GitHubItemWatcher(PR_URL), _view("OPEN"))
        assert result.status == Status.IDLE
        assert not result.newly_actionable

    def test_merge_alerts_once_until_acknowledged(self):
        """Merge alerts on transition, then stays unacknowledged without re-firing."""
        watcher = GitHubItemWatcher(PR_URL)
        _check(watcher, _view("OPEN"))
        merged = _check(watcher, _view("MERGED"))
        assert merged.status == Status.SUCCESS and merged.newly_actionable
        again = _check(watcher, _view("MERGED"))
        assert again.unacknowledged and not again.newly_actionable
        watcher.acknowledge()
        assert _check(watcher, _view("MERGED")).unacknowledged is False

    def test_closed_pr_is_failure_and_closed_issue_is_success(self):
        """Closing a PR unmerged is a failure; closing an issue is a success."""
        assert _check(GitHubItemWatcher(PR_URL), _view("CLOSED")).status == Status.FAILURE
        assert _check(GitHubItemWatcher(ISSUE_URL), _view("CLOSED")).status == Status.SUCCESS

    def test_already_merged_on_add_alerts(self):
        """First poll of an already-merged PR still notifies."""
        assert _check(GitHubItemWatcher(PR_URL), _view("MERGED")).newly_actionable

    def test_gh_error_reports_error(self):
        """A failing gh call surfaces as an error status."""
        watcher = GitHubItemWatcher(PR_URL)
        with patch("watcher.watchers.github_item._run_gh_view", side_effect=RuntimeError("boom")):
            assert watcher.check().status == Status.ERROR


class TestActivityTrigger:
    """The any-activity trigger."""

    def test_first_poll_sets_baseline(self):
        """The first look is a baseline, not an alert."""
        result = _check(GitHubItemWatcher(PR_URL, trigger="activity"), _view())
        assert result.status == Status.IDLE and not result.newly_actionable

    def test_update_alerts_and_acknowledge_rearms(self):
        """A changed updatedAt alerts; after acknowledging, the next change alerts again."""
        watcher = GitHubItemWatcher(PR_URL, trigger="activity")
        _check(watcher, _view(updated="t1"))
        hit = _check(watcher, _view(updated="t2"))
        assert hit.status == Status.SUCCESS and hit.newly_actionable
        watcher.acknowledge()
        assert _check(watcher, _view(updated="t2")).status == Status.IDLE
        assert _check(watcher, _view(updated="t3")).newly_actionable


class TestChecksTrigger:
    """The checks trigger delegates to the PR watcher."""

    def test_delegates_to_pr_watcher(self):
        """Resolved checks alert through the inner PR watcher."""
        watcher = GitHubItemWatcher(PR_URL, trigger="checks")
        data = {
            "headRefOid": "abc",
            "state": "OPEN",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        }
        with patch("watcher.watchers.github_pr._run_gh_pr_view", return_value=data), patch(
            "watcher.watchers.github_pr._fetch_copilot_events", return_value=[]
        ), patch("watcher.watchers.github_pr._count_runs_awaiting_approval", return_value=0), patch(
            "watcher.watchers.github_pr._count_copilot_review_runs_in_progress", return_value=0
        ):
            result = watcher.check()
        assert result.status == Status.SUCCESS and result.newly_actionable
        assert watcher.last_head_sha == "abc"
        restored = GitHubItemWatcher.from_config(watcher.to_config())
        assert restored.trigger == "checks" and restored.last_head_sha == "abc"


def test_config_round_trip():
    """Trigger and markers survive serialization."""
    watcher = GitHubItemWatcher(ISSUE_URL, trigger="activity", last_key="a", acknowledged_key="b")
    restored = GitHubItemWatcher.from_config(watcher.to_config())
    assert (restored.trigger, restored.last_key, restored.acknowledged_key) == ("activity", "a", "b")
    assert restored.category == "long_term"


class _Stub:
    def __init__(self, wid, interval=None):
        self.id = wid
        self.label = wid
        self.poll_interval = interval
        self.calls = 0

    def check(self):
        self.calls += 1
        return CheckResult(status=Status.IDLE)


class TestSchedulerIntervals:
    """Per-watcher poll intervals."""

    def test_long_interval_watcher_skipped_until_due(self):
        """A watcher with its own interval isn't re-polled every round unless forced."""
        fast, slow = _Stub("fast"), _Stub("slow", interval=3600)
        sched = Scheduler()
        sched.set_watchers([fast, slow])
        sched._poll_all()
        sched._poll_all()
        assert (fast.calls, slow.calls) == (2, 1)
        sched._poll_all(force=True)
        assert (fast.calls, slow.calls) == (3, 2)


class TestEngineLongTerm:
    """Engine add/update/persist of long-term watchers."""

    @pytest.fixture
    def engine(self, tmp_path, monkeypatch):
        """A quiet engine with a temp config."""
        monkeypatch.setattr("watcher.core.scheduler.Scheduler.poll_once_async", lambda self, force=False: None)
        return Engine(config_path=tmp_path / "config.json", check_updates=False, deliver_notifications=False)

    def test_add_long_term_defaults_and_persists(self, engine, tmp_path):
        """Long-term adds default to the closed trigger and a 10-minute interval, and reload intact."""
        snap = engine.add_watcher(PR_URL, category="long_term")
        assert (snap["category"], snap["trigger"], snap["poll_interval"]) == ("long_term", "closed", 600)
        reloaded = Engine(config_path=tmp_path / "config.json", check_updates=False, deliver_notifications=False)
        (restored,) = reloaded.list_watchers()
        assert restored["category"] == "long_term" and restored["poll_interval"] == 600

    def test_immediate_pr_unchanged(self, engine):
        """Default adds still use the immediate PR watcher with no interval override."""
        snap = engine.add_watcher(PR_URL)
        assert (snap["type"], snap["category"], snap["poll_interval"]) == ("github_pr", "immediate", None)

    def test_validation(self, engine):
        """Bad category, trigger, URL and interval are rejected."""
        with pytest.raises(ValueError):
            engine.add_watcher(PR_URL, category="weekly")
        with pytest.raises(ValueError):
            engine.add_watcher(ISSUE_URL, category="long_term", trigger="checks")
        with pytest.raises(ValueError):
            engine.add_watcher("https://example.com/x", category="long_term")
        with pytest.raises(ValueError):
            engine.add_watcher(PR_URL, category="long_term", poll_interval=0)
        with pytest.raises(ValueError):
            engine.add_watcher(PR_URL, trigger="closed")

    def test_update_trigger_and_interval(self, engine):
        """Changing the trigger rebuilds the watcher in place; the interval is editable."""
        snap = engine.add_watcher(PR_URL, category="long_term")
        updated = engine.update_watcher(snap["id"], trigger="activity", poll_interval=120)
        assert updated["id"] == snap["id"]
        assert (updated["trigger"], updated["poll_interval"], updated["category"]) == ("activity", 120, "long_term")
