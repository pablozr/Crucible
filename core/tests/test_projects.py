from __future__ import annotations

import json
import subprocess
import uuid

import pytest

from crucible_core.services.projects import ProjectError, resolve_project


def repository(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    return tmp_path


def test_resolves_initialized_project_with_default_config(tmp_path):
    root = repository(tmp_path)
    directory = root / ".crucible"
    directory.mkdir()
    project_id = str(uuid.uuid4())
    (directory / "project.json").write_text(
        json.dumps({"project_id": project_id}), encoding="utf-8"
    )
    project = resolve_project(root)
    assert project.id == project_id
    assert project.max_snapshot_file_size_bytes == 1_048_576


def test_rejects_uninitialized_and_invalid_configuration(tmp_path):
    root = repository(tmp_path)
    with pytest.raises(ProjectError, match="PROJECT_NOT_INITIALIZED"):
        resolve_project(root)
    directory = root / ".crucible"
    directory.mkdir()
    (directory / "project.json").write_text(
        json.dumps({"project_id": str(uuid.uuid4())}), encoding="utf-8"
    )
    (directory / "config.yaml").write_text("version: 2\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="INVALID_PROJECT_CONFIG"):
        resolve_project(root)


def test_rejects_invalid_project_metadata(tmp_path):
    root = repository(tmp_path)
    directory = root / ".crucible"
    directory.mkdir()
    (directory / "project.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ProjectError, match="INVALID_PROJECT_METADATA"):
        resolve_project(root)
