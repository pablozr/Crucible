from __future__ import annotations

from crucible_core.core.config import Settings
from crucible_core.core.database import database_status
from crucible_core.version import VERSION


def operational_status(settings: Settings) -> dict[str, object]:
    return {
        "status": "operational",
        "version": VERSION,
        "api_version": "v1",
        "address": f"http://{settings.host}:{settings.port}",
        "database": database_status(settings.database_path),
    }
