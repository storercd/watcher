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

import shutil
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
