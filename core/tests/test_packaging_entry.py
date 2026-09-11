from __future__ import annotations

import crucible_core.server as server
from crucible_core.main import app as app_object
from crucible_core.version import VERSION


def test_parse_args_defaults_to_server_mode():
    assert server.parse_args([]).version is False


def test_parse_args_version_flag():
    assert server.parse_args(["--version"]).version is True


def test_version_prints_and_exits_without_startup(monkeypatch, capsys):
    def _forbidden(*args, **kwargs):
        raise AssertionError("must not start up")

    monkeypatch.setattr(server.multiprocessing, "freeze_support", _forbidden)
    monkeypatch.setattr(server.uvicorn, "run", _forbidden)
    server.main(["--version"])
    assert capsys.readouterr().out.strip() == VERSION


def test_server_runs_app_object_with_settings(monkeypatch):
    calls: dict[str, object] = {}
    monkeypatch.setattr(server.multiprocessing, "freeze_support", lambda: None)
    monkeypatch.setattr(server, "configure_logging", lambda: None)

    class _Settings:
        host = "127.0.0.1"
        port = 7331

    monkeypatch.setattr(server, "load_settings", lambda: _Settings())

    def _fake_run(run_app, **kwargs):
        calls["app"] = run_app
        calls["kwargs"] = kwargs

    monkeypatch.setattr(server.uvicorn, "run", _fake_run)
    server.main([])

    assert calls["app"] is app_object
    assert calls["kwargs"] == {"host": "127.0.0.1", "port": 7331}
