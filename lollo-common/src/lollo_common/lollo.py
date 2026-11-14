"""The lollo's look: lollo.css, which every lollo page links to."""

from __future__ import annotations

from importlib import resources


def stylesheet() -> bytes:
    return (resources.files("lollo_common") / "static" / "lollo.css").read_bytes()
