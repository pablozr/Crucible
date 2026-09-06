from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from crucible_core.main import app


def test_health_and_status(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        health = client.get("/v1/health")
        status = client.get("/v1/status")
    assert health.json() == {"status": "ok", "version": "0.1.0", "api_version": "v1"}
    assert status.json()["database"] == {
        "path": str(tmp_path / "crucible.db"),
        "migration_revision": "0001",
        "journal_mode": "wal",
        "synchronous": "full",
        "foreign_keys": True,
    }


def test_unknown_api_route_is_problem_json(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.get("/v1/unknown")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "API route not found",
        "status": 404,
        "detail": "No API route matches the request.",
        "instance": "/v1/unknown",
        "code": "API_ROUTE_NOT_FOUND",
    }


def test_unknown_api_method_is_problem_json(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.post("/v1/health")
    assert response.status_code == 405
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "API_METHOD_NOT_ALLOWED"


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
        INSERT INTO sessions VALUES ('session', 'tree', 'adapter', 'session');
        INSERT INTO tasks VALUES ('task-one', 'session', 'tree', 'running');
        """
    )
    try:
        connection.execute("INSERT INTO tasks VALUES ('task-two', 'session', 'tree', 'finalizing')")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("expected active task uniqueness constraint")
