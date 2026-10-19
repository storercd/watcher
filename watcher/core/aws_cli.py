"""
Shared helpers for locating and invoking the ``aws`` CLI.

Like ``gh_cli``, this exists because a GUI-launched ``.app`` gets a bare
``PATH`` that usually lacks Homebrew's ``bin`` directories.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

_FALLBACK_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/local/aws-cli",
    str(Path.home() / ".local" / "bin"),
)


@lru_cache(maxsize=1)
def find_aws() -> Optional[str]:
    """
    Locate the ``aws`` executable beyond a GUI app's bare ``PATH``.

    Returns:
        The resolved path, or ``None`` if it can't be found.
    """
    found = shutil.which("aws")
    if found:
        return found
    for directory in _FALLBACK_DIRS:
        candidate = Path(directory) / "aws"
        if candidate.is_file() and candidate.stat().st_mode & 0o111:
            return str(candidate)
    return None


def aws_command(*args: str) -> list:
    """
    Build an ``aws`` argv, falling back to the literal ``"aws"``.

    Returns:
        ``[resolved_path_or_"aws", *args]``.
    """
    return [find_aws() or "aws", *args]


def list_profiles(timeout: int = 10) -> List[str]:
    """
    List the AWS CLI profiles configured on this machine.

    Returns:
        Profile names (empty if the CLI is missing or none are configured).
    """
    try:
        proc = subprocess.run(
            aws_command("configure", "list-profiles"),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
