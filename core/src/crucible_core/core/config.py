from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 7331

    @property
    def database_path(self) -> Path:
        return self.data_dir / "crucible.db"


def load_settings() -> Settings:
    override = os.environ.get("CRUCIBLE_DATA_DIR")
    data_dir = Path(override) if override else Path(user_data_path("Crucible", appauthor=False))
    return Settings(data_dir=data_dir.expanduser().resolve())
