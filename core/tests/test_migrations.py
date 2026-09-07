from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine


def _upgrade(path, revision: str) -> None:
    config = Config()
    config.set_main_option(
        "script_location",
        "src/crucible_core/migrations",
    )
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def test_migrations_upgrade_fresh_database_to_head(tmp_path):
    database_path = tmp_path / "fresh.db"
    _upgrade(database_path, "head")
    _assert_head_tables(database_path)


def test_migrations_upgrade_supported_previous_versions_to_head(tmp_path):
    for revision in ("0001", "0002", "0003", "0004"):
        database_path = tmp_path / f"{revision}.db"
        _upgrade(database_path, revision)
        if revision == "0001":
            _insert_legacy_task(database_path)
        _upgrade(database_path, "head")
        _assert_head_tables(database_path)


def _assert_head_tables(database_path) -> None:
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'candidate_baseline_files'"
        ).fetchone()
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        decisions = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' "
            "AND name = 'admission_no_input_decisions'"
        ).fetchone()
        final_columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(task_file_changes)"
            ).fetchall()
        }
    assert row
    assert revision == "0005"
    assert decisions
    assert {"path", "final_content", "patch"} <= final_columns


def _insert_legacy_task(database_path) -> None:
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO projects VALUES ('project', '/repo')"
        )
        connection.exec_driver_sql(
            "INSERT INTO working_trees VALUES ('tree', 'project', '/repo')"
        )
        connection.exec_driver_sql(
            "INSERT INTO sessions VALUES ('session', 'tree', 'adapter', 'session')"
        )
        connection.exec_driver_sql(
            "INSERT INTO tasks VALUES ('task', 'session', 'tree', 'completed')"
        )
