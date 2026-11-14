"""Logging to stdout. Item text from websites is never logged: only source names,
counts and short reasons."""

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
    for name in ("httpcore", "httpx", "jwt", "uvicorn.access", "mcp", "dateparser", "tzlocal"):
        logging.getLogger(name).setLevel(logging.WARNING)
