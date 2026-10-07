"""Tests for watcher.core.update_checker."""

from __future__ import annotations

import json
import urllib.error
from unittest import mock

from watcher.core.update_checker import (
    ReleaseInfo,
    _parse_version,
    check_for_update,
    fetch_latest_release,
    is_newer,
)


def test_parse_version_basic():
    """A plain dotted version string parses into a numeric tuple."""
    assert _parse_version("1.2.3") == (1, 2, 3)


def test_parse_version_strips_leading_v():
    """A leading 'v' (as in git tags) is stripped before parsing."""
    assert _parse_version("v0.1.47") == (0, 1, 47)


def test_parse_version_malformed_falls_back_to_zero():
    """Garbage input never raises; it just looks oldest."""
    assert _parse_version("not-a-version") == (0,)


def test_is_newer_true_when_candidate_greater():
    """A strictly greater version tuple is newer."""
    assert is_newer("v0.1.48", "0.1.47") is True


def test_is_newer_false_when_equal():
    """An identical version is not considered newer."""
    assert is_newer("0.1.47", "0.1.47") is False


def test_is_newer_true_when_base_version_bumped_regardless_of_build_number():
    """A manual base-version bump always beats any build number under the old base."""
    # A manual base-version bump (e.g. "0.2") must always beat any build
    # number under the old base ("0.1.999"), since tuple comparison is
    # lexicographic: (0, 2, 0) > (0, 1, 999).
    assert is_newer("0.2.0", "0.1.999") is True


def test_fetch_latest_release_parses_response():
    """A successful API response is parsed into a ReleaseInfo."""
    payload = json.dumps(
        {"tag_name": "v0.1.47", "html_url": "https://example.com/releases/v0.1.47", "name": "Watcher v0.1.47"}
    ).encode("utf-8")

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self):
            return payload

    with mock.patch("urllib.request.urlopen", return_value=_FakeResponse()):
        release = fetch_latest_release("owner/repo")

    assert release == ReleaseInfo(
        version="v0.1.47", html_url="https://example.com/releases/v0.1.47", name="Watcher v0.1.47"
    )


def test_fetch_latest_release_returns_none_on_network_error():
    """A network failure is swallowed, not raised."""
    with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
        assert fetch_latest_release("owner/repo") is None


def test_check_for_update_returns_none_when_not_newer():
    """No update is reported when the latest release isn't newer."""
    release = ReleaseInfo(version="0.1.47", html_url="https://example.com", name="Watcher v0.1.47")
    with mock.patch("watcher.core.update_checker.fetch_latest_release", return_value=release):
        assert check_for_update("0.1.47") is None


def test_check_for_update_returns_release_when_newer():
    """The release is returned when it's newer than current."""
    release = ReleaseInfo(version="0.1.48", html_url="https://example.com", name="Watcher v0.1.48")
    with mock.patch("watcher.core.update_checker.fetch_latest_release", return_value=release):
        assert check_for_update("0.1.47") == release
