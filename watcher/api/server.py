"""
Localhost-only HTTP/JSON API (plus a Server-Sent Events stream) over the engine.

Security model: the server binds to 127.0.0.1 only, requires a per-launch
bearer token on every request, and rejects requests whose ``Host`` header is
not a loopback name (blocking DNS-rebinding from web pages). The contract is
documented in ``docs/api.yaml``.
"""

from __future__ import annotations

import hmac
import json
import logging
import queue
import re
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple

from watcher import __full_version__
from watcher.core.engine import Engine

logger = logging.getLogger("watcher.api")

API_PREFIX = "/v1"
MAX_BODY_BYTES = 64 * 1024
SSE_HEARTBEAT_SECONDS = 15.0
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
WATCHER_PATH = re.compile(rf"^{API_PREFIX}/watchers/([^/]+)(/acknowledge)?$")


def _host_name(host_header: str) -> str:
    """
    Strip the port from a Host header value.

    Returns:
        The host name, keeping brackets for IPv6 literals.
    """
    if host_header.startswith("["):
        return host_header.split("]", 1)[0] + "]"
    return host_header.split(":", 1)[0]


class ApiError(Exception):
    """An error that maps directly to an HTTP status and JSON error body."""

    def __init__(self, status: HTTPStatus, message: str):
        """Store the HTTP status and client-visible message."""
        super().__init__(message)
        self.status = status
        self.message = message


class WatcherApiServer(ThreadingHTTPServer):
    """HTTP server bound to loopback, carrying the engine and auth token."""

    daemon_threads = True

    def __init__(self, engine: Engine, token: Optional[str] = None, port: int = 0):
        """
        Bind to 127.0.0.1 on ``port`` (0 picks a free port).

        Args:
            engine: The engine to expose.
            token: Bearer token to require; a random one is generated if omitted.
            port: TCP port, or 0 for any free port.
        """
        super().__init__(("127.0.0.1", port), _Handler)
        self.engine = engine
        self.token = token or secrets.token_urlsafe(32)
        self.shutdown_requested = threading.Event()

    @property
    def port(self) -> int:
        """The port actually bound."""
        return self.server_address[1]


class _Handler(BaseHTTPRequestHandler):
    server: WatcherApiServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("%s - %s", self.address_string(), format % args)

    # -- plumbing -------------------------------------------------------
    def _send_json(self, status: HTTPStatus, body: Any) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorize(self) -> None:
        if _host_name(self.headers.get("Host") or "") not in ALLOWED_HOSTS:
            raise ApiError(HTTPStatus.FORBIDDEN, "Invalid Host header")
        header = self.headers.get("Authorization") or ""
        supplied = header[len("Bearer "):] if header.startswith("Bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, self.server.token):
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Missing or invalid bearer token")

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length") from exc
        if length > MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large")
        if length == 0:
            return {}
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Body must be a JSON object")
        return body

    def _dispatch(self, method: str) -> None:
        try:
            self._authorize()
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if method == "GET" and path == f"{API_PREFIX}/events":
                self._stream_events()
                return
            status, body = self._route(method, path)
            if status is HTTPStatus.NO_CONTENT:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send_json(status, body)
        except ApiError as err:
            self._send_json(err.status, {"error": err.message})
        except Exception:  # noqa: BLE001 - never leak internals or kill the server thread
            logger.exception("unhandled error serving %s %s", method, self.path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error"})

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    # -- routing --------------------------------------------------------
    def _route(self, method: str, path: str) -> Tuple[HTTPStatus, Any]:
        engine = self.server.engine
        simple: Dict[Tuple[str, str], Callable[[], Tuple[HTTPStatus, Any]]] = {
            ("GET", f"{API_PREFIX}/health"): lambda: (HTTPStatus.OK, {"status": "ok", "version": __full_version__}),
            ("GET", f"{API_PREFIX}/state"): lambda: (HTTPStatus.OK, engine.get_state()),
            ("GET", f"{API_PREFIX}/watchers"): lambda: (HTTPStatus.OK, engine.list_watchers()),
            ("POST", f"{API_PREFIX}/watchers"): self._add_watcher,
            ("POST", f"{API_PREFIX}/poll"): self._poll,
            ("GET", f"{API_PREFIX}/settings"): lambda: (HTTPStatus.OK, engine.get_settings()),
            ("PATCH", f"{API_PREFIX}/settings"): self._patch_settings,
            ("POST", f"{API_PREFIX}/update/dismiss"): self._dismiss_update,
            ("POST", f"{API_PREFIX}/shutdown"): self._shutdown,
        }
        handler = simple.get((method, path))
        if handler is not None:
            return handler()

        match = WATCHER_PATH.match(path)
        if match:
            return self._watcher_route(method, match.group(1), bool(match.group(2)))
        raise ApiError(HTTPStatus.NOT_FOUND, "Not found")

    def _watcher_route(self, method: str, watcher_id: str, is_ack: bool) -> Tuple[HTTPStatus, Any]:
        engine = self.server.engine
        if is_ack:
            if method != "POST":
                raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "Use POST")
            if engine.get_watcher(watcher_id) is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "Unknown watcher")
            snapshot = engine.acknowledge(watcher_id) or engine.get_watcher(watcher_id)
            return HTTPStatus.OK, snapshot
        if method == "GET":
            snapshot = engine.get_watcher(watcher_id)
            if snapshot is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "Unknown watcher")
            return HTTPStatus.OK, snapshot
        if method == "PATCH":
            return HTTPStatus.OK, self._patch_watcher(watcher_id)
        if method == "DELETE":
            if not engine.remove_watcher(watcher_id):
                raise ApiError(HTTPStatus.NOT_FOUND, "Unknown watcher")
            return HTTPStatus.NO_CONTENT, None
        raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")

    def _patch_watcher(self, watcher_id: str) -> Dict[str, Any]:
        body = self._read_json()
        self._require_strings(body, ("url", "label", "notes"))
        try:
            return self.server.engine.update_watcher(
                watcher_id, url=body.get("url"), label=body.get("label"), notes=body.get("notes")
            )
        except KeyError as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, "Unknown watcher") from exc
        except ValueError as exc:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc

    @staticmethod
    def _require_strings(body: Dict[str, Any], keys: Tuple[str, ...]) -> None:
        for key in keys:
            if key in body and not isinstance(body[key], str):
                raise ApiError(HTTPStatus.BAD_REQUEST, f"'{key}' must be a string")

    # -- handlers -------------------------------------------------------
    def _add_watcher(self) -> Tuple[HTTPStatus, Any]:
        body = self._read_json()
        self._require_strings(body, ("url", "label", "notes"))
        if not body.get("url"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "'url' is required")
        try:
            snapshot = self.server.engine.add_watcher(body["url"], body.get("label", ""), body.get("notes", ""))
        except ValueError as exc:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
        return HTTPStatus.CREATED, snapshot

    def _poll(self) -> Tuple[HTTPStatus, Any]:
        self.server.engine.poll_now()
        return HTTPStatus.ACCEPTED, {"status": "polling"}

    def _patch_settings(self) -> Tuple[HTTPStatus, Any]:
        try:
            return HTTPStatus.OK, self.server.engine.update_settings(self._read_json())
        except ValueError as exc:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc

    def _dismiss_update(self) -> Tuple[HTTPStatus, Any]:
        body = self._read_json()
        version = body.get("version")
        if not isinstance(version, str) or not version:
            raise ApiError(HTTPStatus.BAD_REQUEST, "'version' is required")
        self.server.engine.dismiss_update(version)
        return HTTPStatus.NO_CONTENT, None

    def _shutdown(self) -> Tuple[HTTPStatus, Any]:
        self.server.shutdown_requested.set()
        return HTTPStatus.ACCEPTED, {"status": "shutting down"}

    # -- server-sent events ---------------------------------------------
    def _stream_events(self) -> None:
        events: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=1000)

        def enqueue(event: Dict[str, Any]) -> None:
            try:
                events.put_nowait(event)
            except queue.Full:
                logger.warning("dropping SSE event for slow client")

        unsubscribe = self.server.engine.subscribe(enqueue)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self._write_sse("state", self.server.engine.get_state())
            while not self.server.shutdown_requested.is_set():
                try:
                    event = events.get(timeout=SSE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._write_sse(event["type"], event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            unsubscribe()

    def _write_sse(self, event_type: str, data: Any) -> None:
        self.wfile.write(f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
        self.wfile.flush()
