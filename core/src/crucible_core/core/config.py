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
    terminal_max_authorization_window_seconds: int | None = None

    @property
    def database_path(self) -> Path:
        return self.data_dir / "crucible.db"


DEFAULT_TERMINAL_MAX_AUTHORIZATION_WINDOW_SECONDS = 2


def _parse_terminal_window(raw: str | None) -> int | None:
    try:
        parsed = int(raw.strip())
    except (ValueError, AttributeError):
        return None
    return parsed if parsed > 0 else None


def load_settings() -> Settings:
    override = os.environ.get("CRUCIBLE_DATA_DIR")
    data_dir = (
        Path(override)
        if override
        else Path(user_data_path("Crucible", appauthor=False))
    )
    raw = os.environ.get("CRUCIBLE_TERMINAL_MAX_AUTH_WINDOW_SECONDS")
    window = _parse_terminal_window(raw)

    # An absent env falls back to the tight default window; an invalid or
    # non-positive value stays None so authorization fails closed
    # downstream (TERMINAL_AUTHORIZATION_UNCONFIGURED).
    if window is None and raw is None:
        window = DEFAULT_TERMINAL_MAX_AUTHORIZATION_WINDOW_SECONDS
    return Settings(
        data_dir=data_dir.expanduser().resolve(),
        terminal_max_authorization_window_seconds=window,
    )
