"""Logging to stdout. Nothing here logs email content: subjects and senders stay
in /data, and bodies are never fetched."""

from __future__ import annotations

import logging
import sys


def setup() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # httpx logs every request URL at INFO, and could log more at DEBUG.
    for name in ("httpcore", "httpx", "jwt", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
