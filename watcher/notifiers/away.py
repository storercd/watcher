"""
Away notifier backed by `ntfy <https://ntfy.sh/>`_.

Used when the user is in "Away" mode. Publishes a message to a configurable
ntfy topic (on ntfy.sh or a self-hosted server) via a plain HTTP POST, so the
user can get a push notification to their phone via the ntfy app subscribed
to the same topic. If no topic is configured, falls back to logging so the
app still works before the user has set one up.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request

from watcher.notifiers.base import Notifier

logger = logging.getLogger("watcher.notifiers.away")

DEFAULT_NTFY_SERVER = "https://ntfy.sh"
REQUEST_TIMEOUT_SECONDS = 10


class AwayNotifier(Notifier):
    """Publishes away-from-desk notifications to a configurable ntfy topic."""

    name = "away"

    def __init__(self, server: str = DEFAULT_NTFY_SERVER, topic: str = ""):
        """Initialize with the ntfy server URL and topic to publish to."""
        self.server = server
        self.topic = topic

    def configure(self, server: str, topic: str) -> None:
        """Update the ntfy server/topic this notifier publishes to."""
        self.server = server
        self.topic = topic

    def notify(self, title: str, message: str) -> None:
        """POST a notification to the configured ntfy topic, if any."""
        topic = self.topic.strip()
        if not topic:
            print(f"[away-notifier] no ntfy topic configured; dropping -> {title}: {message}")
            return
        url = self.server.rstrip("/") + "/" + topic
        request = urllib.request.Request(
            url,
            data=message.encode("utf-8"),
            headers={"Title": title},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS):
                pass
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            print(f"[away-notifier] failed to publish to ntfy topic {topic!r}: {exc}")
            logger.debug("ntfy publish failure for topic %r", topic, exc_info=True)
