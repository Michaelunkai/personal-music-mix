"""Application entrypoint: ``python -m app.main``."""

from __future__ import annotations

import uvicorn

from .config import get_settings
from .api import create_app


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
