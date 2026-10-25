"""Text from outside (email headers, the executor) made safe to store and show."""

from __future__ import annotations

import re
from typing import Any

CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")
# Lone surrogates survive JSON decoding ("\ud800") but can't be encoded as
# UTF-8: SQLite and json.dumps(ensure_ascii=False) would raise on them.
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def encodable(text: str) -> str:
    return _SURROGATE.sub("\ufffd", text)


def one_line(text: Any, limit: int) -> str:
    """Control characters and newlines become spaces; at most `limit` characters."""
    if not isinstance(text, str):
        return ""
    flat = " ".join(CONTROL.sub(" ", encodable(text)).split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."
