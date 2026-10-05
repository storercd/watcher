"""Registry mapping ``watcher_type`` strings to Watcher subclasses.

New watcher plugins register themselves here (see watchers/jenkins.py) so the
config loader can reconstruct the right class without the core module needing
to import every plugin directly.
"""

from __future__ import annotations

from typing import Dict, Type

from watcher.core.base import Watcher

_REGISTRY: Dict[str, Type[Watcher]] = {}


def register(watcher_cls: Type[Watcher]) -> Type[Watcher]:
    """Class decorator: register a Watcher subclass under its watcher_type."""
    _REGISTRY[watcher_cls.watcher_type] = watcher_cls
    return watcher_cls


def get_watcher_class(watcher_type: str) -> Type[Watcher]:
    try:
        return _REGISTRY[watcher_type]
    except KeyError as exc:
        raise ValueError(f"Unknown watcher_type: {watcher_type!r}") from exc


def available_types() -> Dict[str, Type[Watcher]]:
    return dict(_REGISTRY)
