"""
Background check for newer Watcher releases on GitHub.

Polls the GitHub Releases API for the repo's latest published release and
reports (via a thread-safe queue, mirroring ``Scheduler``) when it's newer
than the running version, so the GUI can show a "no action needed, but an
update is available" banner without blocking or touching Tkinter off its own
thread.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional, Tuple

from watcher.core.net import ssl_context

logger = logging.getLogger("watcher.update_checker")

GITHUB_REPO = "storercd/watcher"
LATEST_RELEASE_URL_TEMPLATE = "https://api.github.com/repos/{repo}/releases/latest"
REQUEST_TIMEOUT_SECONDS = 10

# How often to re-check for a new release while the app keeps running.
DEFAULT_CHECK_INTERVAL_SECONDS = 60 * 60


@dataclass
class ReleaseInfo:
    """The subset of a GitHub release relevant to update checking."""

    version: str
    html_url: str
    name: str


def _parse_version(raw: str) -> Tuple[int, ...]:
    """
    Parse a version string like ``v1.2.3`` into a comparable tuple.

    Returns:
        A tuple of the numeric components, e.g. ``(1, 2, 3)``. Falls back to
        ``(0,)`` for anything with no digits, so a malformed tag never
        crashes the comparison - it just never looks "newer".
    """
    stripped = raw.strip()
    if stripped.lower().startswith("v"):
        stripped = stripped[1:]
    parts = re.findall(r"\d+", stripped)
    if not parts:
        return (0,)
    return tuple(int(p) for p in parts)


def is_newer(candidate: str, current: str) -> bool:
    """Return True if the ``candidate`` version string is newer than ``current``."""
    return _parse_version(candidate) > _parse_version(current)


def _get_github_token() -> Optional[str]:
    """
    Find a GitHub token to authenticate the releases request with.

    ``GITHUB_REPO`` is a private repository, so an unauthenticated request
    to the releases API gets a 404 (GitHub hides private repos from anonymous
    callers) rather than the actual release data. Without a token, update
    checks would silently and permanently find nothing.

    Checks ``GITHUB_TOKEN``/``GH_TOKEN`` first, then falls back to asking the
    locally installed ``gh`` CLI for its cached auth token.

    Returns:
        The discovered token, or ``None`` (not raising) if neither source
        yields one, e.g. ``gh`` isn't installed or the user isn't logged in -
        the caller then simply makes an unauthenticated request, which still
        works for a public repo.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("gh auth token unavailable: %s", exc)
        return None
    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    return token or None


def fetch_latest_release(
    repo: str = GITHUB_REPO, timeout: float = REQUEST_TIMEOUT_SECONDS
) -> Optional[ReleaseInfo]:
    """
    Fetch the latest published (non-draft, non-prerelease) release for ``repo``.

    Returns:
        A ``ReleaseInfo`` for the latest release, or ``None`` if the request
        fails or the repo has no releases yet (e.g. offline, rate-limited,
        404) - a failed check is silent, never surfaced as an error.
    """
    url = LATEST_RELEASE_URL_TEMPLATE.format(repo=repo)
    headers = {"Accept": "application/vnd.github+json"}
    token = _get_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        logger.debug("update check request failed: %s", exc)
        return None

    tag_name = data.get("tag_name")
    html_url = data.get("html_url")
    if not tag_name or not html_url:
        return None
    return ReleaseInfo(version=tag_name, html_url=html_url, name=data.get("name") or tag_name)


def check_for_update(current_version: str, repo: str = GITHUB_REPO) -> Optional[ReleaseInfo]:
    """
    Check whether a newer release than ``current_version`` has been published.

    Returns:
        The latest ``ReleaseInfo`` if it's newer than ``current_version``,
        otherwise ``None`` (including when the check itself failed).
    """
    release = fetch_latest_release(repo)
    if release is None:
        return None
    if not is_newer(release.version, current_version):
        return None
    return release


class UpdateChecker:
    """Periodically checks for a newer release on a background thread."""

    def __init__(
        self,
        current_version: str,
        repo: str = GITHUB_REPO,
        check_interval: float = DEFAULT_CHECK_INTERVAL_SECONDS,
    ):
        """Initialize with the running version, repo slug, and re-check interval (seconds)."""
        self.current_version = current_version
        self.repo = repo
        self.check_interval = check_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.results: "queue.Queue[ReleaseInfo]" = queue.Queue()

    def start(self) -> None:
        """Start the background check thread if it isn't already running."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="watcher-update-check")
        self._thread.start()

    def stop(self) -> None:
        """Signal the background check thread to stop."""
        self._stop_event.set()

    def _run(self) -> None:
        # Checks once right away, then again every check_interval until stopped.
        while not self._stop_event.is_set():
            try:
                release = check_for_update(self.current_version, self.repo)
            except Exception:  # noqa: BLE001 - a failed check must never kill this thread
                logger.exception("update check failed")
                release = None
            if release is not None:
                self.results.put(release)
            self._stop_event.wait(self.check_interval)
