"""Entry point: python -m garden_vault"""

from __future__ import annotations

import sys

import uvicorn
from garden_common.auth import Authenticator

from . import logs
from .app import create_app
from .config import ConfigError, Settings
from .push import Pusher
from .service import Service
from .vault import Vault


def main() -> None:
    logs.setup()
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"vault: {e}", file=sys.stderr)
        sys.exit(2)
    vault = Vault(settings.db_path)
    service = Service(vault, Pusher(settings, vault), settings.public_origin)
    app = create_app(service, Authenticator(settings), settings.vapid_public_key)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=None,
        # No access log: paths alone are harmless, but nothing here needs them.
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
