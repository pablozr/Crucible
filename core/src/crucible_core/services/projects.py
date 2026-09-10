from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import yaml

from crucible_core.core.errors import ProjectError
from crucible_core.logging import get_logger
from crucible_core.schemas.projects import Project

logger = get_logger(__name__)


def resolve_project(directory: Path) -> Project:
    try:
        root = Path(
            subprocess.run(
                ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        ).resolve()
    except subprocess.CalledProcessError as error:
        logger.warning("project resolution failed code=NOT_A_GIT_REPOSITORY")
        raise ProjectError("NOT_A_GIT_REPOSITORY") from error

    crucible_directory = root / ".crucible"

    if crucible_directory.is_symlink():
        raise ProjectError("INVALID_PROJECT_METADATA")

    project_file = crucible_directory / "project.json"

    if not project_file.is_file():
        raise ProjectError("PROJECT_NOT_INITIALIZED")

    try:
        metadata = json.loads(project_file.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError
        project_id = metadata["project_id"]

        if set(metadata) != {"project_id"} or not isinstance(project_id, str):
            raise ValueError

        uuid.UUID(project_id)
    except (ValueError, KeyError, json.JSONDecodeError) as error:
        logger.warning("project metadata failed code=INVALID_PROJECT_METADATA")
        raise ProjectError("INVALID_PROJECT_METADATA") from error

    limit = 1_048_576
    config_file = crucible_directory / "config.yaml"

    if config_file.is_file():
        try:
            config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            logger.warning("project config failed code=INVALID_PROJECT_CONFIG")
            raise ProjectError("INVALID_PROJECT_CONFIG") from error

        if not isinstance(config, dict) or config.get("version") != 1:
            raise ProjectError("INVALID_PROJECT_CONFIG")

        tracking = config.get("tracking", {})

        if not isinstance(tracking, dict):
            raise ProjectError("INVALID_PROJECT_CONFIG")

        if "max_snapshot_file_size_bytes" in tracking:
            limit = tracking["max_snapshot_file_size_bytes"]

            if (
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or limit <= 0
            ):
                raise ProjectError("INVALID_PROJECT_CONFIG")

    return Project(
        id=project_id,
        git_root=str(root),
        max_snapshot_file_size_bytes=limit,
    )
