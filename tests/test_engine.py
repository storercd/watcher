"""Tests for the headless Engine."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from watcher.core import registry
from watcher.core.base import CheckResult, Status, Watcher
from watcher.core.config import load_config
from watcher.core.engine import Engine


class FakeWatcher(Watcher):
    """A watcher that never touches the network."""

    watcher_type = "fake"

    def __init__(self, url: str, watcher_id=None, label: str = "", notes: str = ""):
        """Store the fake URL."""
        super().__init__(watcher_id=watcher_id, label=label, notes=notes)
        self.url = url

    @classmethod
    def matches_url(cls, url: str) -> bool:
        """Match ``fake://`` URLs."""
        return url.startswith("fake://")

    @property
    def display_url(self):
        """The fake URL."""
        return self.url

    def check(self) -> CheckResult:
        """Always report idle."""
        return CheckResult(status=Status.IDLE)

    def to_config(self) -> Dict[str, Any]:
        """Serialize to a config dict."""
        return {
            "watcher_type": self.watcher_type,
            "id": self.id,
            "label": self.label,
            "notes": self.notes,
            "url": self.url,
        }

    @classmethod
    def from_config(cls, data: Dict[str, Any]) -> "FakeWatcher":
        """Rebuild from a config dict."""
        return cls(data["url"], watcher_id=data["id"], label=data["label"], notes=data.get("notes", ""))


@pytest.fixture(autouse=True)
def fake_type():
    """Register the fake watcher type for each test."""
    registry.register(FakeWatcher)
    yield
    registry._REGISTRY.pop("fake", None)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Build an engine with no network polling or notification delivery."""
    monkeypatch.setattr("watcher.core.scheduler.Scheduler.poll_once_async", lambda self: None)
    return Engine(config_path=tmp_path / "config.json", check_updates=False, deliver_notifications=False)


def test_add_watcher_persists_and_publishes(engine, tmp_path):
    """Adding a watcher saves it to config and emits watcher_added."""
    events = []
    engine.subscribe(events.append)

    snap = engine.add_watcher("fake://one", label="One", notes="n")

    assert snap["label"] == "One" and snap["url"] == "fake://one" and snap["status"] == "unknown"
    assert [e["type"] for e in events] == ["watcher_added"]
    saved = load_config(tmp_path / "config.json")
    assert [w["id"] for w in saved["watchers"]] == [snap["id"]]


def test_add_unrecognized_url_raises(engine):
    """An unrecognized URL is rejected with ValueError."""
    with pytest.raises(ValueError):
        engine.add_watcher("https://example.com/nope")


def test_update_watcher_label_keeps_state_and_url_change_resets(engine):
    """Renaming keeps status; changing the URL resets it but keeps the id."""
    snap = engine.add_watcher("fake://one", label="One")
    engine._handle_result(snap["id"], CheckResult(status=Status.BUILDING, detail="x"))

    renamed = engine.update_watcher(snap["id"], label="Renamed")
    assert renamed["label"] == "Renamed" and renamed["status"] == "building"

    moved = engine.update_watcher(snap["id"], url="fake://two")
    assert moved["url"] == "fake://two" and moved["status"] == "unknown" and moved["id"] == snap["id"]

    with pytest.raises(KeyError):
        engine.update_watcher("missing", label="x")


def test_remove_watcher(engine):
    """Removing returns True once, then False."""
    snap = engine.add_watcher("fake://one")
    assert engine.remove_watcher(snap["id"]) is True
    assert engine.remove_watcher(snap["id"]) is False
    assert engine.list_watchers() == []


def test_newly_actionable_result_emits_notification_and_acknowledge_clears(engine):
    """A newly actionable result emits a notification event until acknowledged."""
    snap = engine.add_watcher("fake://one", label="One")
    watcher = engine.watchers[snap["id"]]
    events = []
    engine.subscribe(events.append)

    watcher.unacknowledged = True
    engine._handle_result(
        snap["id"], CheckResult(status=Status.SUCCESS, detail="done", newly_actionable=True, unacknowledged=True)
    )

    kinds = [e["type"] for e in events]
    assert kinds == ["watcher_updated", "notification"]
    assert events[1]["title"] == "Watcher: One" and "success" in events[1]["message"]
    assert engine.has_unacknowledged()

    acked = engine.acknowledge(snap["id"])
    assert acked["unacknowledged"] is False and not engine.has_unacknowledged()
    assert engine.acknowledge(snap["id"]) is None


def test_engine_delivers_notifications_when_enabled(tmp_path, monkeypatch):
    """With deliver_notifications=True the engine calls the router itself."""
    monkeypatch.setattr("watcher.core.scheduler.Scheduler.poll_once_async", lambda self: None)
    engine = Engine(config_path=tmp_path / "c.json", check_updates=False, deliver_notifications=True)
    sent = []
    monkeypatch.setattr(engine.router, "notify", lambda title, message: sent.append((title, message)))
    snap = engine.add_watcher("fake://one", label="One")

    engine._handle_result(snap["id"], CheckResult(status=Status.FAILURE, detail="boom", newly_actionable=True))

    assert sent == [("Watcher: One", "failure — boom")]


def test_settings_roundtrip_and_validation(engine, tmp_path):
    """Settings persist, and invalid keys or values are rejected."""
    settings = engine.update_settings(
        {"mode": "Away", "ntfy_topic": "t", "poll_interval": 30, "font_scale": 1.5, "renotify_on_restart": False}
    )
    assert settings["mode"] == "Away" and settings["ntfy_topic"] == "t" and settings["poll_interval"] == 30
    assert settings["font_scale"] == 1.5 and settings["renotify_on_restart"] is False

    saved = load_config(tmp_path / "config.json")
    assert saved["mode"] == "Away" and saved["poll_interval"] == 30 and saved["font_scale"] == 1.5

    for bad in ({"nope": 1}, {"mode": "Bogus"}, {"poll_interval": 0}, {"poll_interval": True}):
        with pytest.raises(ValueError):
            engine.update_settings(bad)


def test_watchers_reload_from_config(tmp_path, monkeypatch):
    """A new engine on the same config restores the saved watchers."""
    monkeypatch.setattr("watcher.core.scheduler.Scheduler.poll_once_async", lambda self: None)
    first = Engine(config_path=tmp_path / "c.json", check_updates=False, deliver_notifications=False)
    snap = first.add_watcher("fake://one", label="One")

    second = Engine(config_path=tmp_path / "c.json", check_updates=False, deliver_notifications=False)
    assert [w["id"] for w in second.list_watchers()] == [snap["id"]]
