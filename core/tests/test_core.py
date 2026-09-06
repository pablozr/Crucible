from __future__ import annotations

import logging
import sqlite3

from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

import crucible_core.routes.system as system_routes
from crucible_core.main import app


def test_health_and_status(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        health = client.get("/v1/health")
        status = client.get("/v1/status")
    assert health.json() == {
        "status": "ok",
        "message": "Service is healthy.",
        "data": {
            "health": {
                "status": "ok",
                "version": "0.1.0",
                "api_version": "v1",
            }
        },
    }
    assert status.json()["data"]["system"]["database"] == {
        "path": str(tmp_path / "crucible.db"),
        "migration_revision": "0003",
        "journal_mode": "wal",
        "synchronous": "full",
        "foreign_keys": True,
    }


def test_unknown_api_route_is_envelope_error(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.get("/v1/unknown")
    assert response.status_code == 404
    assert response.json() == {
        "status": "error",
        "message": "API route not found.",
        "data": {"code": "API_ROUTE_NOT_FOUND"},
    }


def test_unknown_api_method_is_envelope_error(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.post("/v1/health")
    assert response.status_code == 405
    assert response.json() == {
        "status": "error",
        "message": "API method not allowed.",
        "data": {"code": "API_METHOD_NOT_ALLOWED"},
    }


def test_unexpected_api_error_is_envelope_error(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.get("/v1/tasks/not-a-uuid")
    assert response.status_code == 422
    assert response.json() == {
        "status": "error",
        "message": "Request validation failed.",
        "data": {"code": "REQUEST_VALIDATION_FAILED"},
    }


def test_exact_v1_path_is_envelope_error(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.get("/v1")
    assert response.status_code == 404
    assert response.json() == {
        "status": "error",
        "message": "API route not found.",
        "data": {"code": "API_ROUTE_NOT_FOUND"},
    }


def test_unexpected_api_error_logs_and_returns_safe_envelope(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path))

    def _boom(settings):
        raise RuntimeError("secret db failure")

    monkeypatch.setattr(system_routes, "operational_status", _boom)
    with caplog.at_level(logging.ERROR, logger="crucible_core"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/v1/status")
    assert response.status_code == 500
    assert response.json() == {
        "status": "error",
        "message": "Unexpected server error.",
        "data": {"code": "INTERNAL_SERVER_ERROR"},
    }
    assert "secret db failure" not in response.text
    assert any(
        record.name.startswith("crucible_core")
        and record.levelno == logging.ERROR
        for record in caplog.records
    )


def test_api_http_exception_uses_safe_envelope_and_headers(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path))

    def _teapot(settings):
        raise StarletteHTTPException(
            status_code=418,
            detail="secret-detail",
            headers={"x-test": "preserved"},
        )

    monkeypatch.setattr(system_routes, "operational_status", _teapot)
    with TestClient(app) as client:
        response = client.get("/v1/status")
    assert response.status_code == 418
    assert response.json() == {
        "status": "error",
        "message": "Request failed.",
        "data": {"code": "API_REQUEST_FAILED"},
    }
    assert response.headers.get("x-test") == "preserved"
    assert "secret-detail" not in response.text


def test_database_enforces_active_task_constraint(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path))
    with TestClient(app):
        pass
    connection = sqlite3.connect(tmp_path / "crucible.db")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        INSERT INTO projects VALUES ('project', '/repo');
        INSERT INTO working_trees VALUES ('tree', 'project', '/repo');
        INSERT INTO sessions (id, working_tree_id, adapter, agent_session_id)
        VALUES ('session', 'tree', 'adapter', 'session');
        INSERT INTO tasks (id, session_id, working_tree_id, status)
        VALUES ('task-one', 'session', 'tree', 'running');
        """
    )
    try:
        connection.execute(
            (
                "INSERT INTO tasks (id, session_id, working_tree_id, status) "
                "VALUES ('task-two', 'session', 'tree', 'finalizing')"
            )
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("expected active task uniqueness constraint")
