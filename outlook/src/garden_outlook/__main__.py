"""Entry point: python -m garden_outlook"""

from __future__ import annotations

import sys

import uvicorn
from garden_common.auth import Authenticator

from . import logs
from .app import create_app
from .config import ConfigError, Settings
from .service import Service


def main() -> None:
    logs.setup()
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"outlook: {e}", file=sys.stderr)
        sys.exit(2)
    service = Service(settings)
    app = create_app(service, Authenticator(settings), settings.public_origin)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=None,
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
