"""
Headless Watcher engine: everything the app does except draw a window.

The engine owns the watchers, the polling scheduler, the notification router,
config persistence and the update checker. UIs (the native SwiftUI app today, a
Windows shell later) are thin clients: they call the command
methods here (directly in-process, or over the local HTTP API in
``watcher.api``) and subscribe to events to re-render.
"""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from watcher.core.base import CheckResult, Status, Watcher
from watcher.core.config import (
    DEFAULT_CONFIG_PATH,
    load_config,
    save_config,
    watchers_to_config_list,
)
from watcher.core.registry import detect_watcher_class, get_watcher_class
from watcher.core.scheduler import Scheduler
from watcher.core.update_checker import ReleaseInfo, UpdateChecker
from watcher.notifiers.away import DEFAULT_NTFY_SERVER
from watcher.notifiers.router import AT_DESK, MODES, NotificationRouter

# Importing these registers the plugins with the registry.
from watcher.watchers.github_actions_run import GitHubActionsRunWatcher  # noqa: F401
from watcher.watchers.github_pr import GitHubPRWatcher  # noqa: F401
from watcher.watchers.jenkins import JenkinsWatcher  # noqa: F401

logger = logging.getLogger("watcher.engine")

NUDGE_INTERVAL_SECONDS = 5.0
RESULT_POLL_SECONDS = 0.5

UNRECOGNIZED_URL_MESSAGE = (
    "Could not determine what kind of URL this is.\n\n"
    "Expected a Jenkins job URL (contains /job/), a GitHub PR URL "
    "(github.com/owner/repo/pull/123), or a GitHub Actions run URL "
    "(github.com/owner/repo/actions/runs/123456)."
)

STATUS_LABELS = {
    Status.UNKNOWN: "unknown",
    Status.IDLE: "idle",
    Status.BUILDING: "building",
    Status.SUCCESS: "success",
    Status.FAILURE: "failure",
    Status.ERROR: "error",
}

# Preferences that only a UI cares about; the engine just persists them.
UI_SETTING_KEYS = ("font_scale", "always_on_top", "update_dismissed_version")
# Settings the engine acts on.
ENGINE_SETTING_KEYS = ("mode", "ntfy_server", "ntfy_topic", "poll_interval", "renotify_on_restart")
SETTING_KEYS = ENGINE_SETTING_KEYS + UI_SETTING_KEYS

Event = Dict[str, Any]
Subscriber = Callable[[Event], None]


class Engine:
    """Headless core: watchers, polling, notifications, persistence, update checks."""

    def __init__(
        self,
        config_path: Path = DEFAULT_CONFIG_PATH,
        version: str = "",
        deliver_notifications: bool = True,
        check_updates: bool = True,
    ):
        """
        Load config and build (but do not start) the engine.

        Args:
            config_path: Where to load and save the JSON config.
            version: The running app version, used for update checks.
            deliver_notifications: If True the engine fires notifications
                itself from a background thread. A UI that must deliver them
                on its own main thread (e.g. AppKit) passes False and
                reacts to ``notification`` events instead.
            check_updates: Whether to poll GitHub for newer releases.
        """
        self._config_path = config_path
        self._deliver_notifications = deliver_notifications
        self._lock = threading.RLock()
        self._subscribers: List[Subscriber] = []
        self._stop_event = threading.Event()
        self._threads: List[threading.Thread] = []
        self.available_update: Optional[ReleaseInfo] = None

        self.config = load_config(config_path)
        self.scheduler = Scheduler(poll_interval=self.config.get("poll_interval", 15))
        self.router = NotificationRouter(
            mode=self.config.get("mode", AT_DESK),
            ntfy_server=self.config.get("ntfy_server", DEFAULT_NTFY_SERVER),
            ntfy_topic=self.config.get("ntfy_topic", ""),
        )
        self.watchers: Dict[str, Watcher] = {}
        self._load_watchers_from_config()
        if self.config.get("renotify_on_restart", True):
            for watcher in self.watchers.values():
                watcher.forget_acknowledgment()
        self.scheduler.set_watchers(list(self.watchers.values()))

        self.update_checker = UpdateChecker(current_version=version) if check_updates else None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start polling, result handling, nudging and update checking."""
        self._stop_event.clear()
        self.scheduler.start()
        self.scheduler.poll_once_async()
        self._spawn(self._result_loop, "watcher-engine-results")
        if self._deliver_notifications:
            self._spawn(self._nudge_loop, "watcher-engine-nudge")
        if self.update_checker is not None:
            self.update_checker.start()
            self._spawn(self._update_loop, "watcher-engine-updates")

    def stop(self) -> None:
        """Signal every background thread to stop and persist config."""
        self._stop_event.set()
        self.scheduler.stop()
        if self.update_checker is not None:
            self.update_checker.stop()
        self._save()

    def _spawn(self, target: Callable[[], None], name: str) -> None:
        thread = threading.Thread(target=target, daemon=True, name=name)
        thread.start()
        self._threads.append(thread)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """
        Register an event callback.

        Callbacks run on engine threads, so they must be fast and thread-safe
        (e.g. put the event on a queue). Event types: ``watcher_added``,
        ``watcher_updated``, ``watcher_removed``, ``settings_changed``,
        ``update_available``, ``notification``. Each has a ``type`` key plus
        a type-specific payload.

        Returns:
            A function that unsubscribes the callback.
        """
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def _publish(self, event: Event) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - one bad subscriber must not break the engine
                logger.exception("event subscriber failed for %s", event.get("type"))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    @staticmethod
    def snapshot_watcher(watcher: Watcher) -> Dict[str, Any]:
        """
        Describe a watcher's config plus current state as a JSON-safe dict.

        Returns:
            The watcher snapshot.
        """
        return {
            "id": watcher.id,
            "type": watcher.watcher_type,
            "label": watcher.label,
            "notes": watcher.notes,
            "url": watcher.display_url,
            "status": watcher.last_status.value,
            "detail": watcher.last_detail,
            "last_checked": watcher.last_checked,
            "unacknowledged": watcher.unacknowledged,
        }

    def list_watchers(self) -> List[Dict[str, Any]]:
        """
        Snapshot every watcher, in insertion order.

        Returns:
            One snapshot dict per watcher.
        """
        with self._lock:
            return [self.snapshot_watcher(w) for w in self.watchers.values()]

    def get_watcher(self, watcher_id: str) -> Optional[Dict[str, Any]]:
        """
        Snapshot one watcher.

        Returns:
            The snapshot, or None if the id is unknown.
        """
        with self._lock:
            watcher = self.watchers.get(watcher_id)
            return self.snapshot_watcher(watcher) if watcher else None

    def get_settings(self) -> Dict[str, Any]:
        """
        Return the current settings plus the list of valid modes.

        Returns:
            Settings dict; UI-only keys are included when they have been set.
        """
        with self._lock:
            settings: Dict[str, Any] = {
                "mode": self.router.mode,
                "ntfy_server": self.router.away_notifier.server,
                "ntfy_topic": self.router.away_notifier.topic,
                "poll_interval": self.scheduler.poll_interval,
                "renotify_on_restart": bool(self.config.get("renotify_on_restart", True)),
                "modes": list(MODES),
            }
            for key in UI_SETTING_KEYS:
                if key in self.config:
                    settings[key] = self.config[key]
            return settings

    def get_state(self) -> Dict[str, Any]:
        """
        Return everything a UI needs to render from scratch.

        Returns:
            Dict with ``watchers``, ``settings`` and ``update`` (or None).
        """
        update = self.available_update
        return {
            "watchers": self.list_watchers(),
            "settings": self.get_settings(),
            "update": {"version": update.version, "url": update.html_url, "name": update.name} if update else None,
        }

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def add_watcher(self, url: str, label: str = "", notes: str = "") -> Dict[str, Any]:
        """
        Detect the watcher type for ``url``, start watching it, and persist.

        Returns:
            The new watcher's snapshot.
        """
        watcher = self._construct_watcher(url, label, notes)
        with self._lock:
            self.watchers[watcher.id] = watcher
            self.scheduler.add_watcher(watcher)
            self._save()
        snapshot = self.snapshot_watcher(watcher)
        self._publish({"type": "watcher_added", "watcher": snapshot})
        self.scheduler.poll_once_async()
        return snapshot

    def update_watcher(
        self,
        watcher_id: str,
        url: Optional[str] = None,
        label: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Edit a watcher's URL, label and/or notes; omitted fields are kept.

        If the URL changes, the watcher is replaced in place (same id) and
        its status resets, since it now points at something else.

        Returns:
            The updated snapshot. An unknown ``watcher_id`` raises KeyError, and
            an unrecognized or rejected new URL raises ValueError.
        """
        with self._lock:
            watcher = self.watchers[watcher_id]
            new_label = watcher.label if label is None else label
            new_notes = watcher.notes if notes is None else notes
            url_changed = url is not None and url != (watcher.display_url or "")
            if url_changed:
                watcher = self._construct_watcher(url, new_label, new_notes, watcher_id=watcher_id)
                self.watchers[watcher_id] = watcher
                self.scheduler.add_watcher(watcher)
            else:
                watcher.label = new_label.strip() or watcher.default_label()
                watcher.notes = new_notes
            self._save()
            snapshot = self.snapshot_watcher(watcher)
        self._publish({"type": "watcher_updated", "watcher": snapshot})
        if url_changed:
            self.scheduler.poll_once_async()
        return snapshot

    def remove_watcher(self, watcher_id: str) -> bool:
        """
        Stop watching an item and persist.

        Returns:
            True if it existed and was removed.
        """
        with self._lock:
            if self.watchers.pop(watcher_id, None) is None:
                return False
            self.scheduler.remove_watcher(watcher_id)
            self._save()
        self._publish({"type": "watcher_removed", "id": watcher_id})
        return True

    def acknowledge(self, watcher_id: str) -> Optional[Dict[str, Any]]:
        """
        Mark a watcher's current actionable result as seen.

        Returns:
            The updated snapshot, or None if the id is unknown or there was
            nothing to acknowledge.
        """
        with self._lock:
            watcher = self.watchers.get(watcher_id)
            if watcher is None or not watcher.unacknowledged:
                return None
            watcher.acknowledge()
            self._save()
            snapshot = self.snapshot_watcher(watcher)
        self._publish({"type": "watcher_updated", "watcher": snapshot})
        return snapshot

    def poll_now(self) -> None:
        """Trigger an immediate out-of-band poll of every watcher."""
        self.scheduler.poll_once_async()

    def update_settings(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """
        Apply and persist a partial settings update.

        Returns:
            The full settings after the update.

        Raises:
            ValueError: If a key is unknown or a value is invalid.
        """
        unknown = set(changes) - set(SETTING_KEYS)
        if unknown:
            raise ValueError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
        with self._lock:
            if "mode" in changes:
                self.router.set_mode(changes["mode"])
            if "ntfy_server" in changes or "ntfy_topic" in changes:
                self.router.configure_away(
                    server=changes.get("ntfy_server", self.router.away_notifier.server),
                    topic=changes.get("ntfy_topic", self.router.away_notifier.topic),
                )
            if "poll_interval" in changes:
                interval = changes["poll_interval"]
                if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < 1:
                    raise ValueError("poll_interval must be a number >= 1")
                self.scheduler.poll_interval = interval
            if "renotify_on_restart" in changes:
                self.config["renotify_on_restart"] = bool(changes["renotify_on_restart"])
            for key in UI_SETTING_KEYS:
                if key in changes:
                    self.config[key] = changes[key]
            self._save()
            settings = self.get_settings()
        self._publish({"type": "settings_changed", "settings": settings})
        return settings

    def dismiss_update(self, version: str) -> None:
        """Remember that the user dismissed the banner for ``version``."""
        update = self.available_update
        if update is not None and update.version == version:
            self.available_update = None
        self.update_settings({"update_dismissed_version": version})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _construct_watcher(
        self, url: str, label: str, notes: str, watcher_id: Optional[str] = None
    ) -> Watcher:
        watcher_cls = detect_watcher_class(url)
        if watcher_cls is None:
            raise ValueError(UNRECOGNIZED_URL_MESSAGE)
        return watcher_cls(url, watcher_id=watcher_id, label=label, notes=notes)

    def _load_watchers_from_config(self) -> None:
        for entry in self.config.get("watchers", []):
            try:
                watcher = get_watcher_class(entry["watcher_type"]).from_config(entry)
            except (KeyError, ValueError) as exc:
                logger.warning("skipping invalid config entry %r: %s", entry, exc)
                continue
            self.watchers[watcher.id] = watcher

    def _save(self) -> None:
        with self._lock:
            self.config["watchers"] = watchers_to_config_list(list(self.watchers.values()))
            self.config["mode"] = self.router.mode
            self.config["ntfy_server"] = self.router.away_notifier.server
            self.config["ntfy_topic"] = self.router.away_notifier.topic
            self.config["poll_interval"] = self.scheduler.poll_interval
            save_config(self.config, self._config_path)

    def _handle_result(self, watcher_id: str, result: CheckResult) -> None:
        with self._lock:
            watcher = self.watchers.get(watcher_id)
            if watcher is None:
                return
            watcher.last_status = result.status
            watcher.last_detail = result.detail
            watcher.last_checked = result.checked_at
            snapshot = self.snapshot_watcher(watcher)
            if result.newly_actionable:
                self._save()
        self._publish({"type": "watcher_updated", "watcher": snapshot})
        if result.newly_actionable:
            title = f"Watcher: {snapshot['label']}"
            message = f"{STATUS_LABELS.get(result.status, result.status.value)} — {result.detail}"
            self._publish({"type": "notification", "title": title, "message": message, "id": watcher_id})
            if self._deliver_notifications:
                self.router.notify(title=title, message=message)

    def _result_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                watcher_id, result = self.scheduler.results.get(timeout=RESULT_POLL_SECONDS)
            except queue.Empty:
                continue
            try:
                self._handle_result(watcher_id, result)
            except Exception:  # noqa: BLE001 - a bad result must not kill the loop
                logger.exception("failed handling result for %s", watcher_id)

    def has_unacknowledged(self) -> bool:
        """
        Whether any watcher has an actionable result awaiting acknowledgment.

        Returns:
            True if at least one watcher is unacknowledged.
        """
        with self._lock:
            return any(w.unacknowledged for w in self.watchers.values())

    def nudge(self) -> None:
        """Re-assert attention via the notifiers if anything is unacknowledged."""
        try:
            self.router.nudge(self.has_unacknowledged())
        except Exception:  # noqa: BLE001 - a nudge failure must never stop the loop
            logger.exception("router.nudge() failed")

    def _nudge_loop(self) -> None:
        while not self._stop_event.wait(NUDGE_INTERVAL_SECONDS):
            self.nudge()

    def _update_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                release = self.update_checker.results.get(timeout=RESULT_POLL_SECONDS)
            except queue.Empty:
                continue
            if release.version == self.config.get("update_dismissed_version"):
                logger.debug("update %s found but already dismissed", release.version)
                continue
            self.available_update = release
            self._publish(
                {"type": "update_available", "version": release.version, "url": release.html_url, "name": release.name}
            )
