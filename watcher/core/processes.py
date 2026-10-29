"""
Inspect local processes (macOS/Linux ``ps``) for the process watcher and its picker.

Only metadata is read: pid, tty, start time, command line and working
directory. Nothing is read from, or written to, the processes themselves.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Dict, List, Optional

PS_TIMEOUT_SECONDS = 5
SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "nu"}
_NO_TTY = {"?", "??", ""}


def _run(args: List[str]) -> str:
    """
    Run a read-only inspection command and return its stdout.

    Returns:
        The command's stdout, or an empty string if it could not be run.
    """
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=PS_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout


def _parse_start(text: str) -> Optional[float]:
    """
    Convert a ``ps -o lstart`` timestamp (e.g. ``Thu Oct 29 07:44:21 2026``) to epoch seconds.

    Returns:
        Epoch seconds, or None if the text is not a recognized timestamp.
    """
    try:
        return time.mktime(time.strptime(" ".join(text.split()), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def process_start_time(pid: int) -> Optional[float]:
    """
    Start time of a live process.

    The start time identifies a process across pid reuse. Zombies are
    treated as already finished.

    Returns:
        Epoch start time, or None if the pid is not running.
    """
    out = _run(["ps", "-p", str(pid), "-o", "stat=,lstart="]).strip()
    if not out:
        return None
    stat, _, started = out.partition(" ")
    if stat.startswith("Z"):
        return None
    return _parse_start(started)


def _working_directories(pids: List[int]) -> Dict[int, str]:
    """
    Look up the current directory of each pid with one ``lsof`` call.

    Returns:
        A pid -> directory map; pids that could not be read are omitted.
    """
    if not pids:
        return {}
    out = _run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(str(p) for p in pids)])
    result: Dict[int, str] = {}
    pid: Optional[int] = None
    for line in out.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            result[pid] = line[1:]
    return result


def list_terminal_commands() -> List[Dict[str, Any]]:
    """
    List commands currently running in the foreground of a terminal.

    These are the foreground processes started directly by an interactive
    shell, i.e. what a person would see "running" in a terminal window (VS
    Code, Terminal.app, iTerm, ...). Idle shells are not included.

    Returns:
        Dicts with ``pid``, ``tty``, ``started`` (epoch), ``elapsed``
        (seconds), ``command`` and ``cwd``, longest-running first.
    """
    out = _run(["ps", "-axo", "pid=,ppid=,tty=,stat=,lstart=,command="])
    rows: Dict[int, Dict[str, Any]] = {}
    for line in out.splitlines():
        parts = line.split(None, 9)
        if len(parts) < 10 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        started = _parse_start(" ".join(parts[4:9]))
        if started is None:
            continue
        rows[int(parts[0])] = {
            "pid": int(parts[0]),
            "ppid": int(parts[1]),
            "tty": parts[2],
            "stat": parts[3],
            "started": started,
            "command": parts[9].strip(),
        }

    now = time.time()
    found = []
    for row in rows.values():
        parent = rows.get(row["ppid"])
        if (
            parent is not None
            and row["tty"] not in _NO_TTY
            and "+" in row["stat"]
            and not row["stat"].startswith("Z")
            and _is_shell(parent["command"])
            and row["pid"] != os.getpid()
        ):
            found.append(row)

    cwds = _working_directories([row["pid"] for row in found])
    commands = [
        {
            "pid": row["pid"],
            "tty": row["tty"],
            "started": row["started"],
            "elapsed": max(0, int(now - row["started"])),
            "command": row["command"],
            "cwd": cwds.get(row["pid"], ""),
        }
        for row in found
    ]
    return sorted(commands, key=lambda item: item["started"])


def _is_shell(command: str) -> bool:
    """
    Whether a ``ps`` command line is an interactive shell.

    Returns:
        True for a shell binary, including login shells shown as ``-zsh``.
    """
    first = command.split(None, 1)[0] if command.strip() else ""
    return os.path.basename(first).lstrip("-") in SHELLS
