from __future__ import annotations

import uvicorn

from .core.config import load_settings
from .logging import configure_logging


def main() -> None:
    configure_logging()
    settings = load_settings()
    uvicorn.run(
        "crucible_core.main:app", host=settings.host, port=settings.port
    )
