from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    if path.exists() and os.name != "nt":
        path.chmod(0o600)
    return connection


def upgrade(path: Path) -> None:
    with connect(path):
        pass
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parents[1] / "migrations")
    )
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def database_status(path: Path) -> dict[str, object]:
    with connect(path) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        return {
            "path": str(path),
            "migration_revision": revision,
            "journal_mode": connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0],
            "synchronous": "full"
            if connection.execute("PRAGMA synchronous").fetchone()[0] == 2
            else "unknown",
            "foreign_keys": connection.execute(
                "PRAGMA foreign_keys"
            ).fetchone()[0]
            == 1,
        }
