"""Entry point: python -m code_server_gate"""

from __future__ import annotations

import logging
import sys

import httpx
import uvicorn
from garden_common.auth import Authenticator

from .app import create_app
from .config import ConfigError, Settings


def main() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().handlers[:] = [handler]
    logging.getLogger().setLevel(logging.INFO)
    # httpx logs every request URL at INFO; the editor's URLs carry file paths.
    for name in ("httpcore", "httpx", "jwt", "uvicorn.access", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"code-server gate: {e}", file=sys.stderr)
        sys.exit(2)
    # No read timeout: downloads and long requests stream for as long as they take.
    client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None, write=None), follow_redirects=False)
    app = create_app(settings, Authenticator(settings), client)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=None,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        ws_max_size=64 * 1024 * 1024,
    )


if __name__ == "__main__":
    main()
