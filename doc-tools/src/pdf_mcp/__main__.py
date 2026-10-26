"""Entry point: python -m pdf_mcp, started by bin/pdf-mcp for one `letta mcp`
call and stopped after it."""

from __future__ import annotations

import logging
import os
import signal
import sys

from .config import ConfigError, Settings
from .gotenberg import Gotenberg
from .paths import PathError, Workspace, remove_pending
from .server import build_server


def _exit(signum: int, frame: object) -> None:
    # letta-code ends a server with SIGTERM (SIGKILL 2 s later). A tool may be
    # blocked in a worker thread, which would keep a normal exit waiting, so
    # remove what is half-written and leave at once.
    remove_pending()
    os._exit(128 + signum)


def main() -> None:
    # letta-code pipes stderr and never reads it: keep it nearly silent, and
    # never log document content or paths.
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr, format="pdf-mcp: %(message)s")
    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGINT, _exit)
    try:
        settings = Settings.from_env()
        ws = Workspace(settings.workspace)
    except (ConfigError, PathError) as e:
        print(f"pdf-mcp: {e}", file=sys.stderr)
        sys.exit(2)
    gotenberg = Gotenberg(settings.gotenberg_url, settings.timeout_seconds, settings.max_file_bytes)
    try:
        build_server(settings, ws, gotenberg).run()
    finally:
        gotenberg.close()
        ws.close()


if __name__ == "__main__":
    main()
