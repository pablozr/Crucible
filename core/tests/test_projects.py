from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path

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


def _contract_root() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "test"
        / "fixtures"
        / "project-contract"
    )


def test_shared_project_contract_manifest(tmp_path):
    root = _contract_root()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["cases"]

    for case in manifest["cases"]:
        repo = tmp_path / case["id"]
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
        directory = repo / ".crucible"
        directory.mkdir()
        shutil.copyfile(root / case["projectFile"], directory / "project.json")
        if case["configFile"] is not None:
            shutil.copyfile(
                root / case["configFile"], directory / "config.yaml"
            )

        if case["expected"] == "ok":
            project = resolve_project(repo)
            stored = json.loads(
                (directory / "project.json").read_text(encoding="utf-8")
            )
            assert project.id == stored["project_id"], case["id"]
        else:
            with pytest.raises(ProjectError, match=case["expected"]):
                resolve_project(repo)
