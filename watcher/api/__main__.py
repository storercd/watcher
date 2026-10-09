"""
Run the headless Watcher engine with its local API: ``python -m watcher.api``.

On startup it prints one JSON line to stdout, ``{"port": ..., "token": ...}``,
which a UI shell that launched it reads to learn how to connect. It exits on
SIGINT/SIGTERM, on ``POST /v1/shutdown``, or (with ``--exit-on-stdin-close``)
when the parent process goes away and closes stdin.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading

from watcher import __full_version__
from watcher.api.server import WatcherApiServer
from watcher.core.engine import Engine
from watcher.core.logging_config import configure_logging

logger = logging.getLogger("watcher.api")


def _watch_stdin(stop: threading.Event) -> None:
    """Set ``stop`` once stdin reaches EOF, i.e. the launching process died."""
    try:
        while sys.stdin.read(1024):
            pass
    except (OSError, ValueError):
        pass
    stop.set()


def main(argv: list[str] | None = None) -> int:
    """
    Start the engine and API server and block until asked to stop.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description="Watcher headless engine + local API")
    parser.add_argument("--port", type=int, default=0, help="TCP port on 127.0.0.1 (default: any free port)")
    parser.add_argument(
        "--token-env",
        default="WATCHER_API_TOKEN",
        help="Environment variable holding the bearer token (a random one is generated if unset)",
    )
    parser.add_argument("--exit-on-stdin-close", action="store_true", help="Exit when stdin is closed")
    args = parser.parse_args(argv)

    configure_logging()
    engine = Engine(version=__full_version__)
    server = WatcherApiServer(engine, token=os.environ.get(args.token_env) or None, port=args.port)
    engine.start()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    if args.exit_on_stdin_close:
        threading.Thread(target=_watch_stdin, args=(stop,), daemon=True, name="watcher-stdin").start()

    threading.Thread(target=server.serve_forever, daemon=True, name="watcher-api").start()
    print(json.dumps({"port": server.port, "token": server.token}), flush=True)
    logger.info("Watcher API listening on 127.0.0.1:%d", server.port)

    while not (stop.is_set() or server.shutdown_requested.is_set()):
        stop.wait(0.5)

    logger.info("Watcher API shutting down")
    server.shutdown()
    engine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
