from __future__ import annotations

import uvicorn

from .core.config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run("crucible_core.main:app", host=settings.host, port=settings.port)
