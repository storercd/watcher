"""
Local JSON config persistence for Watcher.

Config lives at ``~/.watcher/config.json`` by default and stores the list of
watchers (serialized via ``Watcher.to_config()``) plus app-level settings
(poll interval, notification mode).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_CONFIG_DIR = Path(os.path.expanduser("~/.watcher"))
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.json"

DEFAULT_POLL_INTERVAL = 15
DEFAULT_MODE = "At Desk"
DEFAULT_NTFY_SERVER = "https://ntfy.sh"
DEFAULT_NTFY_TOPIC = ""

_DEFAULT_CONFIG: Dict[str, Any] = {
    "poll_interval": DEFAULT_POLL_INTERVAL,
    "mode": DEFAULT_MODE,
    "ntfy_server": DEFAULT_NTFY_SERVER,
    "ntfy_topic": DEFAULT_NTFY_TOPIC,
    "watchers": [],
}


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    """
    Load config from disk, returning defaults if the file doesn't exist.

    Returns:
        The loaded (or default) config dict.
    """
    if not path.exists():
        return dict(_DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT_CONFIG)

    merged = dict(_DEFAULT_CONFIG)
    merged.update(data)
    merged.setdefault("watchers", [])
    return merged


def save_config(config: Dict[str, Any], path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Atomically write config to disk, creating the parent dir if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2, sort_keys=True)
    tmp_path.replace(path)


def watchers_to_config_list(watchers: List) -> List[Dict[str, Any]]:
    """
    Serialize a list of Watcher instances for storage.

    Each Watcher's ``to_config()`` is expected to already include
    ``watcher_type``, ``id``, and ``label`` alongside its own fields.

    Returns:
        A list of config dicts, one per watcher.
    """
    return [w.to_config() for w in watchers]
