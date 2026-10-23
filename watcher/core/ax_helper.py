"""
Locate and run the ``watcher-ax`` helper.

The helper is a small Swift executable that reads other apps' progress bars
through the macOS Accessibility API.

Only macOS has an implementation; :func:`is_supported` lets callers warn on
other platforms instead of silently never alerting.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

HELPER_NAME = "watcher-ax"
HELPER_ENV = "WATCHER_AX_HELPER"


class AxHelperError(Exception):
    """The helper is missing, failed, or lacks the permission it needs."""


def is_supported() -> bool:
    """
    Whether this OS has an app-progress implementation.

    Returns:
        True on macOS.
    """
    return sys.platform == "darwin"


def unsupported_message() -> str:
    """
    Explain that app progress tracking isn't available here.

    Returns:
        A user-facing warning naming the current platform.
    """
    return f"App progress tracking is not supported on {sys.platform} yet (macOS only)"


def find_helper() -> Optional[str]:
    """
    Locate the helper executable.

    Checks ``$WATCHER_AX_HELPER``, the packaged ``Watcher.app/Contents/MacOS``,
    the SwiftPM build directories in a source checkout, then ``PATH``.

    Returns:
        The helper path, or ``None`` if it can't be found.
    """
    candidates = []
    if os.environ.get(HELPER_ENV):
        candidates.append(Path(os.environ[HELPER_ENV]))
    exe = Path(sys.executable).resolve()
    # <app>/Contents/Resources/watcher-core/watcher-core -> <app>/Contents/MacOS
    if len(exe.parents) > 2:
        candidates.append(exe.parents[2] / "MacOS" / HELPER_NAME)
    repo_macos = Path(__file__).resolve().parents[2] / "macos" / ".build"
    candidates += [repo_macos / "release" / HELPER_NAME, repo_macos / "debug" / HELPER_NAME]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(HELPER_NAME)


def run_helper(*args: str, timeout: float = 20) -> Dict[str, Any]:
    """
    Run the helper and parse its JSON output.

    Returns:
        The decoded JSON object.

    Raises:
        AxHelperError: If the helper is missing, times out, reports an error
            (such as denied Accessibility permission), or prints bad output.
    """
    helper = find_helper()
    if helper is None:
        raise AxHelperError("watcher-ax helper not found (build it with `swift build` in macos/)")
    try:
        proc = subprocess.run([helper, *args], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AxHelperError(f"watcher-ax failed: {exc}") from exc
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        detail = proc.stderr.strip() or proc.stdout[:200]
        raise AxHelperError(f"watcher-ax returned unreadable output: {detail}") from exc
    if not isinstance(data, dict):
        raise AxHelperError("watcher-ax returned unexpected output")
    if "error" in data:
        raise AxHelperError(str(data.get("message") or data["error"]))
    return data


def read_progress(bundle_id: str, window_title: str = "") -> Dict[str, Any]:
    """
    Read the progress indicators of a running app.

    Returns:
        ``{"running": bool, "windows": int, "progress": [{"value", "min", "max", "title"}...]}``.
    """
    args = ["progress", bundle_id]
    if window_title:
        args += ["--window-title", window_title]
    return run_helper(*args)


def list_apps() -> list:
    """
    List running GUI apps for an app picker.

    Returns:
        ``[{"bundle_id": ..., "name": ...}, ...]``, empty on unsupported platforms or helper failure.
    """
    if not is_supported():
        return []
    try:
        return list(run_helper("apps").get("apps", []))
    except AxHelperError:
        return []
