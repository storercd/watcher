"""Jenkins job watcher plugin.

Polls a Jenkins job's JSON API (``<job_url>/api/json``) anonymously and
reports its build status. Detects state transitions and newly completed
builds (by tracking the last seen build number) so the scheduler only flags
``newly_actionable`` once per completed build.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from watcher.core.base import CheckResult, Status, Watcher
from watcher.core.registry import register

REQUEST_TIMEOUT_SECONDS = 10


def _fetch_job_json(job_url: str) -> Dict[str, Any]:
    """Fetch and parse ``<job_url>/api/json`` anonymously. Raises on failure."""
    url = job_url.rstrip("/") + "/api/json"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _status_from_job_json(data: Dict[str, Any]) -> (Status, str, Optional[int]):
    """Translate Jenkins job JSON into a (Status, detail, build_number) tuple."""
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
    ):
        self.job_url = job_url.rstrip("/")
        super().__init__(watcher_id=watcher_id, label=label)
        # The last build number we've already notified about; None means
        # "we haven't checked yet" so the very first check never notifies.
        self.last_build_number = last_build_number
        self._has_checked = last_build_number is not None

    def default_label(self) -> str:
        # Use the trailing path segment of the job URL, e.g. ".../job/my-job" -> "my-job"
        return self.job_url.rstrip("/").split("/")[-1] or self.job_url

    def check(self) -> CheckResult:
        try:
            data = _fetch_job_json(self.job_url)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            self.last_status = Status.ERROR
            self.last_detail = str(exc)
            return CheckResult(status=Status.ERROR, detail=str(exc))

        status, detail, build_number = _status_from_job_json(data)

        newly_actionable = False
        if (
            status.is_actionable
            and self._has_checked
            and build_number is not None
            and build_number != self.last_build_number
        ):
            newly_actionable = True

        if build_number is not None and status != Status.BUILDING:
            # Only "commit" the build number once the build has finished, so
            # we don't miss a transition if we happen to poll mid-build.
            self.last_build_number = build_number
        self._has_checked = True

        self.last_status = status
        self.last_detail = detail
        return CheckResult(status=status, detail=detail, newly_actionable=newly_actionable)

    def to_config(self) -> Dict[str, Any]:
        return {
            "watcher_type": self.watcher_type,
            "id": self.id,
            "label": self.label,
            "job_url": self.job_url,
            "last_build_number": self.last_build_number,
        }

    @classmethod
    def from_config(cls, data: Dict[str, Any]) -> "JenkinsWatcher":
        return cls(
            job_url=data["job_url"],
            watcher_id=data.get("id"),
            label=data.get("label", ""),
            last_build_number=data.get("last_build_number"),
        )
