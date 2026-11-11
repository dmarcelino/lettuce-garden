"""Logging with a last line of defence: the access and refresh tokens are
registered here and scrubbed from any log record that happens to contain one.
Nothing is supposed to log a token; this catches mistakes, not design."""

from __future__ import annotations

import logging
import sys
import threading

REDACTED = "[REDACTED]"
# Shorter values are too common to redact without mangling logs; a token is long.
MIN_REDACT_LENGTH = 12


class SecretFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._forms: list[str] = []

    def protect(self, values: object) -> None:
        """Replace the set of strings to scrub (the current tokens)."""
        collected: set[str] = set()
        if isinstance(values, (list, tuple, set)):
            for value in values:
                if isinstance(value, str) and len(value) >= MIN_REDACT_LENGTH:
                    collected.add(value)
        with self._lock:
            self._forms = sorted(collected, key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        with self._lock:
            forms = list(self._forms)
        if not forms:
            return True
        message = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        cleaned, exc = message, record.exc_text
        for form in forms:
            cleaned = cleaned.replace(form, REDACTED)
            exc = exc.replace(form, REDACTED) if exc else exc
        if cleaned != message or exc != record.exc_text:
            record.msg, record.args, record.exc_text, record.exc_info = cleaned, None, exc, None
        return True


SECRET_FILTER = SecretFilter()


def setup() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(SECRET_FILTER)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for name in ("httpcore", "httpx", "urllib3", "jwt", "mcp", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
