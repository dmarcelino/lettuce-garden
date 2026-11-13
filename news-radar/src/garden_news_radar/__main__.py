"""Entry point: python -m garden_news_radar"""

from __future__ import annotations

import sys

import uvicorn
from garden_common.auth import Authenticator

from . import logs
from .app import create_app
from .config import ConfigError, Settings
from .fetch import Fetcher
from .service import Service
from .store import Store


def main() -> None:
    logs.setup()
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"news-radar: {e}", file=sys.stderr)
        sys.exit(2)
    service = Service(Store(settings.db_path), Fetcher(), settings)
    app = create_app(service, Authenticator(settings), settings.executor_token)
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
