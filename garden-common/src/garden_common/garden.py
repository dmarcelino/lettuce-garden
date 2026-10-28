"""The garden's look: garden.css, which every garden page links to."""

from __future__ import annotations

from importlib import resources


def stylesheet() -> bytes:
    return (resources.files("garden_common") / "static" / "garden.css").read_bytes()
