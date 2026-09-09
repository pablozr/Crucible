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
    for revision in ("0001", "0002", "0003", "0004", "0005", "0006"):
        database_path = tmp_path / f"{revision}.db"
        _upgrade(database_path, revision)
        if revision == "0001":
            _insert_legacy_task(database_path)
        _upgrade(database_path, "head")
        _assert_head_tables(database_path)


def _assert_head_tables(database_path) -> None:
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.connect() as connection:
        row = (
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'candidate_baseline_files'"
            )
            .mappings()
            .fetchone()
        )
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        decisions = (
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' "
                "AND name = 'admission_no_input_decisions'"
            )
            .mappings()
            .fetchone()
        )
        final_columns = {
            item["name"]
            for item in connection.exec_driver_sql(
                "PRAGMA table_info(task_file_changes)"
            )
            .mappings()
            .fetchall()
        }
        baseline_columns = {
            item["name"]
            for item in connection.exec_driver_sql(
                "PRAGMA table_info(task_baseline_files)"
            )
            .mappings()
            .fetchall()
        }
    assert row
    assert revision == "0007"
    assert decisions
    assert {"path", "final_content", "patch"} <= final_columns
    assert {
        "baseline_mode",
        "baseline_gitlink_oid",
        "final_mode",
        "final_gitlink_oid",
    } <= final_columns
    assert {"mode", "gitlink_oid"} <= baseline_columns
    with engine.connect() as connection:
        task_columns = {
            item["name"]
            for item in connection.exec_driver_sql("PRAGMA table_info(tasks)")
            .mappings()
            .fetchall()
        }
    assert {"terminal_observed_at", "capture_not_after"} <= task_columns


def test_migration_terminalizes_legacy_active_without_execution_id(
    tmp_path,
):
    database_path = tmp_path / "legacy.db"
    _upgrade(database_path, "0005")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO projects (id, git_root) VALUES ('project', '/repo')"
        )
        for tree, session, task, status, execution in [
            ("tree-null", "session-null", "task-null", "running", None),
            ("tree-empty", "session-empty", "task-empty", "finalizing", ""),
            ("tree-ok", "session-ok", "task-ok", "running", "execution-1"),
            (
                "tree-done",
                "session-done",
                "task-done",
                "completed",
                None,
            ),
        ]:
            connection.exec_driver_sql(
                "INSERT INTO working_trees (id, project_id, git_root) "
                "VALUES ('" + tree + "', 'project', '/" + tree + "')"
            )
            connection.exec_driver_sql(
                "INSERT INTO sessions (id, working_tree_id, adapter, "
                "agent_session_id) VALUES ('"
                + session
                + "', '"
                + tree
                + "', 'adapter', '"
                + session
                + "')"
            )
            if execution is None:
                connection.exec_driver_sql(
                    "INSERT INTO tasks (id, session_id, working_tree_id, "
                    "status, execution_id) VALUES ('"
                    + task
                    + "', '"
                    + session
                    + "', '"
                    + tree
                    + "', '"
                    + status
                    + "', NULL)"
                )
            else:
                connection.exec_driver_sql(
                    "INSERT INTO tasks (id, session_id, working_tree_id, "
                    "status, execution_id) VALUES ('"
                    + task
                    + "', '"
                    + session
                    + "', '"
                    + tree
                    + "', '"
                    + status
                    + "', '"
                    + execution
                    + "')"
                )
        for event_id, event_type, status, outcome, task in [
            (
                "event-null",
                "task_completed",
                "processing",
                "finalizing",
                "task-null",
            ),
            (
                "event-empty",
                "task_completed",
                "processing",
                "finalizing",
                "task-empty",
            ),
            (
                "event-ok",
                "task_completed",
                "processing",
                "finalizing",
                "task-ok",
            ),
            (
                "event-done",
                "task_completed",
                "accepted",
                "completed",
                "task-done",
            ),
            (
                "event-candidate",
                "input_candidate",
                "processing",
                "candidate",
                "task-null",
            ),
        ]:
            connection.exec_driver_sql(
                "INSERT INTO inbound_events (id, payload_hash, status, "
                "event_type, received_at, outcome, task_id) VALUES ('"
                + event_id
                + "', 'hash-"
                + event_id
                + "', '"
                + status
                + "', '"
                + event_type
                + "', 'now', '"
                + outcome
                + "', '"
                + task
                + "')"
            )
    _upgrade(database_path, "head")
    with engine.connect() as connection:
        rows = {
            item["id"]: (item["status"], item["failure_code"])
            for item in connection.exec_driver_sql(
                "SELECT id, status, failure_code FROM tasks"
            )
            .mappings()
            .fetchall()
        }
    assert rows["task-null"] == (
        "failed",
        "EXECUTION_ID_REQUIRED_LEGACY",
    )
    assert rows["task-empty"] == (
        "failed",
        "EXECUTION_ID_REQUIRED_LEGACY",
    )
    assert rows["task-ok"] == ("running", None)
    assert rows["task-done"] == ("completed", None)
    with engine.connect() as connection:
        events = {
            item["id"]: (
                item["status"],
                item["outcome"],
                item["failure_code"],
            )
            for item in connection.exec_driver_sql(
                "SELECT id, status, outcome, failure_code FROM inbound_events"
            )
            .mappings()
            .fetchall()
        }
    assert events["event-null"] == (
        "rejected",
        "rejected",
        "EXECUTION_ID_REQUIRED_LEGACY",
    )
    assert events["event-empty"] == (
        "rejected",
        "rejected",
        "EXECUTION_ID_REQUIRED_LEGACY",
    )
    assert events["event-ok"] == ("processing", "finalizing", None)
    assert events["event-done"] == ("accepted", "completed", None)
    assert events["event-candidate"] == (
        "processing",
        "candidate",
        None,
    )


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
