"""
AWS Step Functions execution watcher plugin.

Accepts a Step Functions console URL (or a bare execution ARN), distills it
to the execution ARN, and polls ``aws stepfunctions describe-execution``.
Rather than requiring a profile hint, it tries every configured AWS CLI
profile until one can read the execution, then remembers that profile.

Requires the AWS CLI (``aws``) with valid credentials (e.g. ``aws sso login``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from watcher.core.aws_cli import aws_command, list_profiles
from watcher.core.base import CheckResult, Status, Watcher
from watcher.core.registry import register

REQUEST_TIMEOUT_SECONDS = 20

_ARN_RE = re.compile(
    r"arn:aws[a-z-]*:states:(?P<region>[a-z0-9-]+):(?P<account>\d{12}):"
    r"(?:execution|express):(?P<machine>[^:\s/#?]+):(?P<name>[^\s/#?]+)"
)

_FAILING = {"FAILED", "TIMED_OUT", "ABORTED"}


def _parse_execution_arn(text: str) -> Tuple[str, str, str, str]:
    """
    Extract (arn, region, state_machine, execution_name) from a console URL or ARN.

    Raises:
        ValueError: If no execution ARN is found.
    """
    match = _ARN_RE.search(unquote(text))
    if not match:
        raise ValueError(f"Not a recognized Step Functions execution URL or ARN: {text!r}")
    return match.group(0), match.group("region"), match.group("machine"), match.group("name")


def _run_aws(args: List[str], profile: Optional[str], region: str) -> Any:
    """
    Run an aws CLI command and parse its JSON output.

    Raises:
        RuntimeError: On a missing CLI, timeout, non-zero exit, or bad output.
    """
    argv = aws_command(*args, "--region", region, "--output", "json", "--no-cli-pager")
    if profile:
        argv += ["--profile", profile]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "AWS_PAGER": ""},
        )
    except FileNotFoundError as exc:
        raise RuntimeError("aws CLI not found. Install it from https://aws.amazon.com/cli/") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"aws timed out after {REQUEST_TIMEOUT_SECONDS}s") from exc
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"aws exited with code {proc.returncode}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse aws output: {exc}") from exc


def _describe(arn: str, region: str, profile: Optional[str]) -> Dict[str, Any]:
    """Call describe-execution for ``arn`` using ``profile``."""
    return _run_aws(["stepfunctions", "describe-execution", "--execution-arn", arn], profile, region)


def _current_state(arn: str, region: str, profile: Optional[str]) -> Optional[str]:
    """
    Best-effort name of the most recently entered state.

    Returns:
        The state name, or None if it can't be determined.
    """
    try:
        data = _run_aws(
            [
                "stepfunctions", "get-execution-history", "--execution-arn", arn,
                "--reverse-order", "--max-items", "100",
            ],
            profile,
            region,
        )
    except RuntimeError:
        return None
    for event in data.get("events") or []:
        details = event.get("stateEnteredEventDetails")
        if details and details.get("name"):
            return details["name"]
    return None


def _status_from_execution(data: Dict[str, Any], state: Optional[str] = None) -> Tuple[Status, str]:
    """
    Translate a describe-execution response into (Status, detail).

    Returns:
        A tuple of (status, human-readable detail).
    """
    raw = (data.get("status") or "").upper()
    if raw == "RUNNING":
        return Status.BUILDING, f"Running: {state}" if state else "Running"
    if raw == "SUCCEEDED":
        return Status.SUCCESS, "Execution succeeded"
    if raw in _FAILING:
        detail = f"Execution {raw.replace('_', ' ').lower()}"
        if data.get("error"):
            detail += f": {data['error']}"
        return Status.FAILURE, detail
    if raw == "PENDING_REDRIVE":
        return Status.BUILDING, "Pending redrive"
    return Status.UNKNOWN, raw.capitalize() or "Unknown"


@register
class AwsStepFunctionWatcher(Watcher):
    """Watches a single AWS Step Functions execution."""

    watcher_type = "aws_step_function"

    def __init__(
        self,
        execution_arn: str,
        watcher_id: Optional[str] = None,
        label: str = "",
        last_completed: Optional[str] = None,
        acknowledged_completed: Optional[str] = None,
        notes: str = "",
        profile: Optional[str] = None,
    ):
        """Initialize a watcher for the execution ARN (or console URL) given."""
        self.execution_arn, self.region, self.state_machine, self.execution_name = _parse_execution_arn(
            execution_arn
        )
        super().__init__(watcher_id=watcher_id, label=label, notes=notes)
        # Profile that last worked; re-discovered if it stops working.
        self.profile = profile
        # Marker (stop date) of the completion last seen / acknowledged.
        self.last_completed = last_completed
        self.acknowledged_completed = acknowledged_completed
        self._checked_since_start = False

    def default_label(self) -> str:
        """Derive ``<state machine> <short execution id>``."""
        return f"{self.state_machine} {self.execution_name[:8]}"

    @property
    def display_url(self) -> Optional[str]:
        """Console URL for this execution."""
        return (
            f"https://{self.region}.console.aws.amazon.com/states/home?region={self.region}"
            f"#/v2/executions/details/{self.execution_arn}"
        )

    @classmethod
    def matches_url(cls, url: str) -> bool:
        """Whether the text contains a Step Functions execution ARN."""
        return bool(_ARN_RE.search(unquote(url)))

    def _fetch(self) -> Dict[str, Any]:
        """
        Describe the execution, trying the remembered profile then all others.

        Raises:
            RuntimeError: If no profile can read the execution.
        """
        candidates: List[Optional[str]] = []
        if self.profile:
            candidates.append(self.profile)
        for name in list_profiles():
            if name not in candidates:
                candidates.append(name)
        if not candidates:
            candidates.append(None)  # default credential chain / env vars

        errors: List[str] = []
        for profile in candidates:
            try:
                data = _describe(self.execution_arn, self.region, profile)
            except RuntimeError as exc:
                if "not found" in str(exc).lower() and "aws cli" in str(exc).lower():
                    raise
                errors.append(f"{profile or 'default'}: {str(exc).splitlines()[-1] if str(exc) else exc}")
                continue
            self.profile = profile
            data["_profile"] = profile
            return data
        self.profile = None
        raise RuntimeError(
            "No AWS profile could read this execution (try `aws sso login`). " + "; ".join(errors[:3])
        )

    def check(self) -> CheckResult:
        """Poll the execution and report its status."""
        try:
            data = self._fetch()
        except RuntimeError as exc:
            self.last_status = Status.ERROR
            self.last_detail = str(exc)
            return CheckResult(status=Status.ERROR, detail=str(exc))

        state = None
        if (data.get("status") or "").upper() == "RUNNING":
            state = _current_state(self.execution_arn, self.region, data.get("_profile"))
        status, detail = _status_from_execution(data, state)

        marker = str(data.get("stopDate") or data.get("status") or "")
        unacknowledged = status.is_actionable and marker != self.acknowledged_completed
        newly_actionable = unacknowledged and (marker != self.last_completed or not self._checked_since_start)
        self._checked_since_start = True
        if status.is_actionable:
            self.last_completed = marker

        self.last_status = status
        self.last_detail = detail
        self.unacknowledged = unacknowledged
        return CheckResult(
            status=status, detail=detail, newly_actionable=newly_actionable, unacknowledged=unacknowledged
        )

    def acknowledge(self) -> None:
        """Mark the current completion as seen."""
        self.acknowledged_completed = self.last_completed
        self.unacknowledged = False

    def forget_acknowledgment(self) -> None:
        """Clear the acknowledged marker so the last completion re-notifies."""
        self.acknowledged_completed = None

    def to_config(self) -> Dict[str, Any]:
        """Serialize for JSON config storage."""
        return {
            "watcher_type": self.watcher_type,
            "id": self.id,
            "label": self.label,
            "execution_arn": self.execution_arn,
            "last_completed": self.last_completed,
            "acknowledged_completed": self.acknowledged_completed,
            "notes": self.notes,
            "profile": self.profile,
        }

    @classmethod
    def from_config(cls, data: Dict[str, Any]) -> "AwsStepFunctionWatcher":
        """Reconstruct from a dict produced by to_config()."""
        return cls(
            execution_arn=data["execution_arn"],
            watcher_id=data.get("id"),
            label=data.get("label", ""),
            last_completed=data.get("last_completed"),
            acknowledged_completed=data.get("acknowledged_completed"),
            notes=data.get("notes", ""),
            profile=data.get("profile"),
        )
