"""Settings from the environment. bin/pdf-mcp sets them; Lettuce's MCP entry
needs none."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if not lo <= v <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}")
    return v


@dataclass(frozen=True)
class Settings:
    workspace: str
    gotenberg_url: str = "http://gotenberg:3000"
    max_file_bytes: int = 50 * 1024 * 1024
    max_pages: int = 2000
    max_render_dpi: int = 200
    max_output_files: int = 500
    max_text_chars: int = 200_000
    timeout_seconds: int = 45

    @classmethod
    def from_env(cls) -> Settings:
        ws = os.environ.get("PDF_WORKSPACE_ROOT", "")
        if not ws.startswith("/"):
            raise ConfigError("PDF_WORKSPACE_ROOT must be an absolute path (bin/pdf-mcp sets it)")
        url = os.environ.get("GOTENBERG_URL", cls.gotenberg_url)
        if not url.startswith("http://"):
            raise ConfigError("GOTENBERG_URL must be an http:// URL inside the stack")
        return cls(
            workspace=ws,
            gotenberg_url=url.rstrip("/"),
            max_file_bytes=_int("PDF_MAX_FILE_MB", 50, 1, 500) * 1024 * 1024,
            max_pages=_int("PDF_MAX_PAGES", 2000, 1, 100_000),
            max_render_dpi=_int("PDF_MAX_RENDER_DPI", 200, 36, 600),
            max_output_files=_int("PDF_MAX_OUTPUT_FILES", 500, 1, 10_000),
            max_text_chars=_int("PDF_MAX_TEXT_CHARS", 200_000, 1000, 10_000_000),
            # letta-code gives each MCP request 60 s; stay under it so a slow
            # conversion fails here, cleanly, instead of being killed.
            timeout_seconds=_int("PDF_REQUEST_TIMEOUT_SECONDS", 45, 5, 55),
        )
