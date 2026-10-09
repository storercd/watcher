"""Tests for the local HTTP API."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from tests.test_engine import FakeWatcher  # noqa: F401 - also reused via the fixture below
from watcher.api.server import WatcherApiServer
from watcher.core import registry
from watcher.core.engine import Engine

TOKEN = "test-token"


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Serve an engine over the API on a free port."""
    monkeypatch.setattr("watcher.core.scheduler.Scheduler.poll_once_async", lambda self: None)
    registry.register(FakeWatcher)
    engine = Engine(config_path=tmp_path / "config.json", check_updates=False, deliver_notifications=False)
    srv = WatcherApiServer(engine, token=TOKEN)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    registry._REGISTRY.pop("fake", None)


def call(server, method, path, body=None, token=TOKEN, host=None):
    """Make one API request and return (status, decoded JSON or None)."""
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if host:
        headers["Host"] = host
    payload = json.dumps(body) if body is not None else None
    if payload:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, (json.loads(raw) if raw else None)


def test_requires_token(server):
    """Requests without a valid bearer token get 401."""
    assert call(server, "GET", "/v1/state", token=None)[0] == 401
    assert call(server, "GET", "/v1/state", token="wrong")[0] == 401


def test_rejects_non_loopback_host(server):
    """A non-loopback Host header (DNS rebinding) gets 403."""
    assert call(server, "GET", "/v1/state", host="evil.example.com")[0] == 403
    assert call(server, "GET", "/v1/state", host=f"localhost:{server.port}")[0] == 200


def test_watcher_crud_flow(server):
    """Create, read, patch, acknowledge and delete a watcher."""
    status, created = call(server, "POST", "/v1/watchers", {"url": "fake://one", "label": "One"})
    assert status == 201 and created["label"] == "One"
    wid = created["id"]

    assert call(server, "GET", "/v1/watchers")[1][0]["id"] == wid
    assert call(server, "GET", f"/v1/watchers/{wid}")[1]["url"] == "fake://one"

    status, patched = call(server, "PATCH", f"/v1/watchers/{wid}", {"notes": "remember"})
    assert status == 200 and patched["notes"] == "remember"

    status, acked = call(server, "POST", f"/v1/watchers/{wid}/acknowledge")
    assert status == 200 and acked["unacknowledged"] is False

    assert call(server, "DELETE", f"/v1/watchers/{wid}")[0] == 204
    assert call(server, "GET", f"/v1/watchers/{wid}")[0] == 404
    assert call(server, "DELETE", f"/v1/watchers/{wid}")[0] == 404


def test_validation_errors(server):
    """Bad input maps to 400/404/422."""
    assert call(server, "POST", "/v1/watchers", {})[0] == 400
    assert call(server, "POST", "/v1/watchers", {"url": 5})[0] == 400
    assert call(server, "POST", "/v1/watchers", {"url": "https://example.com"})[0] == 422
    assert call(server, "PATCH", "/v1/watchers/missing", {"label": "x"})[0] == 404
    assert call(server, "GET", "/v1/nope")[0] == 404


def test_settings_and_state(server):
    """Settings can be patched and appear in /state."""
    status, settings = call(server, "PATCH", "/v1/settings", {"mode": "Away", "poll_interval": 20})
    assert status == 200 and settings["mode"] == "Away"
    assert call(server, "PATCH", "/v1/settings", {"mode": "Bogus"})[0] == 422
    assert call(server, "PATCH", "/v1/settings", {"bogus": 1})[0] == 422

    state = call(server, "GET", "/v1/state")[1]
    assert state["settings"]["poll_interval"] == 20 and state["watchers"] == [] and state["update"] is None


def test_shutdown_sets_flag(server):
    """POST /v1/shutdown asks the host process to exit."""
    assert call(server, "POST", "/v1/shutdown")[0] == 202
    assert server.shutdown_requested.is_set()


def test_event_stream_sends_state_then_events(server):
    """The SSE stream sends the full state first, then live events."""
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request("GET", "/v1/events", headers={"Authorization": f"Bearer {TOKEN}"})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type") == "text/event-stream"

    def read_event():
        """Read one SSE frame as a dict of field -> value."""
        lines = []
        while True:
            line = resp.fp.readline().decode().rstrip("\n")
            if not line:
                return dict(part.split(": ", 1) for part in lines)
            lines.append(line)

    assert read_event()["event"] == "state"
    call(server, "POST", "/v1/watchers", {"url": "fake://one"})
    event = read_event()
    assert event["event"] == "watcher_added"
    assert json.loads(event["data"])["watcher"]["url"] == "fake://one"
    conn.close()
