from __future__ import annotations

from pathlib import Path

import pytest

from crucible_core.core.config import load_settings

TERMINAL_WINDOW_ENV = "CRUCIBLE_TERMINAL_MAX_AUTH_WINDOW_SECONDS"


def test_absent_env_defaults_to_two_second_window(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(TERMINAL_WINDOW_ENV, raising=False)

    settings = load_settings()

    assert settings.data_dir == Path(str(tmp_path)).expanduser().resolve()
    assert settings.terminal_max_authorization_window_seconds == 2


def test_valid_positive_override_is_retained(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(TERMINAL_WINDOW_ENV, " 45 ")

    settings = load_settings()

    assert settings.terminal_max_authorization_window_seconds == 45


@pytest.mark.parametrize("raw", ["0", "-3", "abc", "1.5", ""])
def test_invalid_override_fails_closed_as_none(monkeypatch, tmp_path, raw):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(TERMINAL_WINDOW_ENV, raw)

    settings = load_settings()

    assert settings.terminal_max_authorization_window_seconds is None
