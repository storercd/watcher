"""Unit tests for the AWS Step Functions watcher (AWS calls mocked)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from watcher.core.base import Status
from watcher.watchers.aws_step_function import (
    AwsStepFunctionWatcher,
    _parse_execution_arn,
    _status_from_execution,
)

ARN = "arn:aws:states:us-west-2:861914951438:execution:my-machine:abc-123"
URL = f"https://x.console.aws.amazon.com/states/home?region=us-west-2#/v2/executions/details/{ARN}"


def test_parse_url_and_bare_arn():
    """Console URLs and bare ARNs both distill to the ARN."""
    assert _parse_execution_arn(URL) == (ARN, "us-west-2", "my-machine", "abc-123")
    assert _parse_execution_arn(ARN)[0] == ARN
    with pytest.raises(ValueError):
        _parse_execution_arn("nope")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("RUNNING", Status.BUILDING),
        ("SUCCEEDED", Status.SUCCESS),
        ("FAILED", Status.FAILURE),
        ("TIMED_OUT", Status.FAILURE),
        ("ABORTED", Status.FAILURE),
    ],
)
def test_status_mapping(raw, expected):
    """Execution statuses map onto normalized statuses."""
    assert _status_from_execution({"status": raw})[0] == expected


def test_falls_back_to_other_profiles_and_remembers():
    """A failing profile is skipped and the working one is remembered."""
    w = AwsStepFunctionWatcher(URL)

    def fake(arn, region, profile):
        if profile == "good":
            return {"status": "SUCCEEDED", "stopDate": "t1"}
        raise RuntimeError("AccessDenied")

    with patch("watcher.watchers.aws_step_function.list_profiles", return_value=["bad", "good"]), patch(
        "watcher.watchers.aws_step_function._describe", side_effect=fake
    ):
        result = w.check()
    assert result.status == Status.SUCCESS and result.newly_actionable
    assert w.profile == "good"
    w.acknowledge()
    assert w.acknowledged_completed == "t1"


def test_all_profiles_fail_is_error():
    """If no profile works the result is an error."""
    w = AwsStepFunctionWatcher(URL)
    with patch("watcher.watchers.aws_step_function.list_profiles", return_value=["a"]), patch(
        "watcher.watchers.aws_step_function._describe", side_effect=RuntimeError("denied")
    ):
        assert w.check().status == Status.ERROR


def test_config_roundtrip():
    """Config serialization round-trips."""
    w = AwsStepFunctionWatcher(URL, profile="p")
    assert AwsStepFunctionWatcher.from_config(w.to_config()).to_config() == w.to_config()
