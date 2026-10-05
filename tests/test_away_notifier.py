"""
Unit tests for the ntfy-backed Away notifier.

HTTP calls are mocked by patching ``urllib.request.urlopen`` so these tests
run offline and fast.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from watcher.notifiers.away import DEFAULT_NTFY_SERVER, AwayNotifier


class TestAwayNotifier:
    """Tests for AwayNotifier's ntfy publishing behavior."""

    def test_no_topic_configured_skips_publish(self):
        """With no topic set, notify() should not attempt any HTTP call."""
        notifier = AwayNotifier(server=DEFAULT_NTFY_SERVER, topic="")
        with patch("urllib.request.urlopen") as mock_urlopen:
            notifier.notify("Title", "Message")
        mock_urlopen.assert_not_called()

    def test_publishes_to_configured_topic(self):
        """notify() should POST to ``<server>/<topic>`` with title header and message body."""
        notifier = AwayNotifier(server="https://ntfy.sh", topic="my-topic")
        mock_response = MagicMock()
        mock_response.__enter__.return_value = mock_response
        with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            notifier.notify("Watcher: build", "success — done")

        assert mock_urlopen.called
        request = mock_urlopen.call_args[0][0]
        assert request.full_url == "https://ntfy.sh/my-topic"
        assert request.get_header("Title") == "Watcher: build"
        assert request.data == b"success \xe2\x80\x94 done"

    def test_configure_updates_server_and_topic(self):
        """configure() should update both the server and topic used by notify()."""
        notifier = AwayNotifier()
        notifier.configure(server="https://custom.example.com", topic="updated-topic")
        assert notifier.server == "https://custom.example.com"
        assert notifier.topic == "updated-topic"

    def test_publish_failure_is_swallowed(self):
        """A network failure during publish should not raise."""
        import urllib.error

        notifier = AwayNotifier(server="https://ntfy.sh", topic="my-topic")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
            notifier.notify("Title", "Message")  # should not raise
