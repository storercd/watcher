"""
Jenkins job watcher plugin.

Polls a Jenkins job's JSON API (``<job_url>/api/json``) anonymously and
reports its build status. Detects state transitions and newly completed
builds (by tracking the last seen build number) so the scheduler flags
``newly_actionable`` once per completed build - and again on every app
restart for as long as that completion remains unacknowledged.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from watcher.core.base import CheckResult, Status, Watcher
from watcher.core.registry import register

REQUEST_TIMEOUT_SECONDS = 10

# Matches the job portion of any Jenkins URL: everything up to and including
# the last consecutive "/job/<name>" pair (folders nest as /job/a/job/b).
# Users paste URLs of sub-pages (a build, its /console, /parameters, ...)
# while looking at a job; those have no job-level api/json shape (or 404), so
# they are normalized back to the job URL.
_JOB_URL_RE = re.compile(r"^(.*?/job/[^/]+(?:/job/[^/]+)*)(?:[/?#].*)?$")


def _normalize_job_url(job_url: str) -> str:
    """
    Reduce any URL under a Jenkins job to the job URL itself.

    ``.../job/my-job/645/console`` and ``.../job/my-job/645`` both become
    ``.../job/my-job``. URLs without a ``/job/<name>`` segment are returned
    with only trailing slashes stripped.

    Returns:
        The job-level URL.
    """
    stripped = job_url.strip().rstrip("/")
    match = _JOB_URL_RE.match(stripped)
    return match.group(1) if match else stripped


def _fetch_job_json(job_url: str) -> Dict[str, Any]:
    """
    Fetch and parse ``<job_url>/api/json`` anonymously. Raises on failure.

    Returns:
        The parsed JSON response body.
    """
    url = job_url.rstrip("/") + "/api/json"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _status_from_job_json(data: Dict[str, Any]) -> (Status, str, Optional[int]):
    """
    Translate Jenkins job JSON into a (Status, detail, build_number) tuple.

    Returns:
        A tuple of (status, detail message, last build number).
    """
    last_build = data.get("lastBuild")
    build_number = last_build.get("number") if last_build else None

    if data.get("color", "").endswith("_anime"):
        # Jenkins appends "_anime" to the color while a build is running.
        return Status.BUILDING, "Building…", build_number

    color = data.get("color", "")
    if color in ("blue", "green"):
        return Status.SUCCESS, "Last build succeeded", build_number
    if color in ("red", "yellow"):
        return Status.FAILURE, "Last build failed or unstable", build_number
    if color == "aborted":
        return Status.FAILURE, "Last build aborted", build_number
    if color == "notbuilt":
        return Status.IDLE, "Never built", build_number
    if color == "disabled":
        return Status.IDLE, "Job disabled", build_number
    return Status.UNKNOWN, f"Unknown color: {color!r}", build_number


@register
class JenkinsWatcher(Watcher):
    """Watches a single Jenkins job for build completion."""

    watcher_type = "jenkins"

    def __init__(
        self,
        job_url: str,
        watcher_id: Optional[str] = None,
        label: str = "",
        last_build_number: Optional[int] = None,
        acknowledged_build_number: Optional[int] = None,
        notes: str = "",
    ):
        """Initialize a Jenkins watcher for the given job URL."""
        self.job_url = _normalize_job_url(job_url)
        super().__init__(watcher_id=watcher_id, label=label, notes=notes)
        # The last completed build number we've seen, used to detect a
        # genuinely new completion.
        self.last_build_number = last_build_number
        # The last completed build number the user has acknowledged (e.g. by
        # clicking the row). A completed build whose number doesn't match
        # this is "unacknowledged" and keeps notifying/highlighting across
        # app restarts until the user acknowledges it.
        self.acknowledged_build_number = acknowledged_build_number
        # In-memory only: whether this watcher has completed at least one
        # check() since the app started, used to re-alert once per restart
        # for a completion that's still unacknowledged.
        self._checked_since_start = False

    def default_label(self) -> str:
        """
        Derive a label from the job URL's trailing path segment.

        e.g. ".../job/my-job" -> "my-job".

        Returns:
            The derived label.
        """
        return self.job_url.rstrip("/").split("/")[-1] or self.job_url

    @property
    def display_url(self) -> Optional[str]:
        """The Jenkins job URL, for opening in a browser."""
        return self.job_url

    @classmethod
    def matches_url(cls, url: str) -> bool:
        """
        Whether a pasted URL looks like a Jenkins job (has a ``/job/`` segment).

        Returns:
            True if the URL contains a ``/job/`` path segment.
        """
        return "/job/" in url.rstrip("/")

    def check(self) -> CheckResult:
        """
        Poll the Jenkins job and report its status/newly-actionable state.

        Returns:
            The result of polling the Jenkins job.
        """
        try:
            data = _fetch_job_json(self.job_url)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            self.last_status = Status.ERROR
            self.last_detail = str(exc)
            return CheckResult(status=Status.ERROR, detail=str(exc))

        status, detail, build_number = _status_from_job_json(data)

        unacknowledged = (
            status.is_actionable
            and build_number is not None
            and build_number != self.acknowledged_build_number
        )
        # Notify when a build newly completes (build_number advanced), or -
        # on the first poll since the app started - when a completion from a
        # prior run is still unacknowledged, so restarting re-alerts instead
        # of staying silent forever.
        newly_actionable = unacknowledged and (
            build_number != self.last_build_number or not self._checked_since_start
        )
        self._checked_since_start = True

        if build_number is not None and status != Status.BUILDING:
            # Only "commit" the build number once the build has finished, so
            # we don't miss a transition if we happen to poll mid-build.
            self.last_build_number = build_number

        self.last_status = status
        self.last_detail = detail
        self.unacknowledged = unacknowledged
        return CheckResult(
            status=status, detail=detail, newly_actionable=newly_actionable, unacknowledged=unacknowledged
        )

    def acknowledge(self) -> None:
        """Mark the current completed build as seen, clearing the unacknowledged state."""
        self.acknowledged_build_number = self.last_build_number
        self.unacknowledged = False

    def forget_acknowledgment(self) -> None:
        """Clear the acknowledged build number so the last completion re-notifies."""
        self.acknowledged_build_number = None

    def to_config(self) -> Dict[str, Any]:
        """
        Serialize this watcher's URL, id, label, and build-tracking state.

        Returns:
            A dict suitable for JSON config storage.
        """
        return {
            "watcher_type": self.watcher_type,
            "id": self.id,
            "label": self.label,
            "job_url": self.job_url,
            "last_build_number": self.last_build_number,
            "acknowledged_build_number": self.acknowledged_build_number,
            "notes": self.notes,
        }

    @classmethod
    def from_config(cls, data: Dict[str, Any]) -> "JenkinsWatcher":
        """
        Reconstruct a JenkinsWatcher from a dict produced by to_config().

        Returns:
            A new JenkinsWatcher instance.
        """
        return cls(
            job_url=data["job_url"],
            watcher_id=data.get("id"),
            label=data.get("label", ""),
            last_build_number=data.get("last_build_number"),
            acknowledged_build_number=data.get("acknowledged_build_number"),
            notes=data.get("notes", ""),
        )
