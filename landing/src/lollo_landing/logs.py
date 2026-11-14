"""Logging to stdout. Agent names are never logged."""

from __future__ import annotations

import logging
import sys


def setup() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for name in ("jwt", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
