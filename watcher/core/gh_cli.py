"""
Shared resolver for locating the ``gh`` CLI executable.

A plain terminal/``python3`` run finds ``gh`` on ``PATH`` because the shell
sources ``.zshrc``/``.bash_profile``, which typically prepend Homebrew's
``bin`` directories. A macOS ``.app`` launched from Finder/Launch Services
is started by ``launchd`` instead, which hands it a bare-bones ``PATH``
(``/usr/bin:/bin:/usr/sbin:/sbin``) with none of that shell setup - so
``subprocess.run(["gh", ...])`` raises ``FileNotFoundError`` even though
``gh`` is installed and works fine from a terminal. Checking a handful of
common install locations as a fallback makes the packaged app see the same
``gh`` the user's shell does.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional

# Common install locations not guaranteed to be on a GUI-launched app's PATH,
# in rough order of likelihood (Homebrew on Apple Silicon, Homebrew on Intel,
# the official .pkg installer, and per-user installs).
_FALLBACK_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/local/share/github-cli/bin",
    str(Path.home() / ".local" / "bin"),
)


@lru_cache(maxsize=1)
def find_gh() -> Optional[str]:
    """
    Locate the ``gh`` executable, beyond what a GUI app's bare ``PATH`` offers.

    Returns:
        The resolved path to ``gh``, or ``None`` if it can't be found
        anywhere - including the fallback locations.
    """
    found = shutil.which("gh")
    if found:
        return found
    for directory in _FALLBACK_DIRS:
        candidate = Path(directory) / "gh"
        if candidate.is_file() and candidate.stat().st_mode & 0o111:
            return str(candidate)
    return None


def gh_command(*args: str) -> list:
    """
    Build a ``gh`` subprocess argv, resolving the executable beyond bare ``PATH``.

    Returns:
        ``[resolved_path_or_"gh", *args]``. Falls back to the literal string
        ``"gh"`` when it can't be resolved, so callers still get the normal
        ``FileNotFoundError`` behavior (and its "not found" messaging) rather
        than a new failure mode.
    """
    return [find_gh() or "gh", *args]


_NO_ACCESS_RE = re.compile(r"Could not resolve to a|HTTP 404|Not Found", re.IGNORECASE)
_ACCOUNT_RE = re.compile(r"Logged in to \S+ account (\S+)")


def _logged_in_accounts(timeout: float) -> list:
    """
    List the distinct ``gh`` account names logged in on github.com.

    Returns:
        Account names, or an empty list if they can't be determined.
    """
    try:
        proc = subprocess.run(
            gh_command("auth", "status", "--hostname", "github.com"),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return list(dict.fromkeys(_ACCOUNT_RE.findall(proc.stdout + proc.stderr)))


def _token_for(account: str, timeout: float) -> Optional[str]:
    try:
        proc = subprocess.run(
            gh_command("auth", "token", "--hostname", "github.com", "--user", account),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    token = proc.stdout.strip()
    return token if proc.returncode == 0 and token else None


def run_gh(*args: str, timeout: float) -> "subprocess.CompletedProcess[str]":
    """
    Run ``gh`` and, if the active account can't see the target, try the others.

    GitHub reports a repository the active account lacks access to as "Could
    not resolve to a Repository" / 404. When several accounts are logged in
    (e.g. a personal and an org account), the active one is often the wrong
    one, so retry with each remaining account's token.

    Returns:
        The first successful result, otherwise the original failed result.

    Raises:
        FileNotFoundError: If ``gh`` isn't installed.
        subprocess.TimeoutExpired: If the initial call times out.
    """
    proc = subprocess.run(
        gh_command(*args), capture_output=True, text=True, timeout=timeout, check=False
    )
    if proc.returncode == 0 or not _NO_ACCESS_RE.search(proc.stderr or ""):
        return proc

    for account in _logged_in_accounts(timeout):
        token = _token_for(account, timeout)
        if not token:
            continue
        env = {**os.environ, "GH_TOKEN": token}
        env.pop("GITHUB_TOKEN", None)
        try:
            retry = subprocess.run(
                gh_command(*args),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            continue
        if retry.returncode == 0:
            return retry
    return proc
