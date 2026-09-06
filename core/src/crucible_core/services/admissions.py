from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Any

from crucible_core.application.admissions import (
    AdmissionCoordinator,
)
from crucible_core.core.database import connect
from crucible_core.core.errors import AdmissionError
from crucible_core.infrastructure.git.baseline_capture import capture_baseline
from crucible_core.logging import get_logger
from crucible_core.repositories import admissions_repository as admissions_repo
from crucible_core.repositories import tasks_repository as tasks_repo
from crucible_core.responses.admissions import (
    event_detail,
    task_detail,
    task_summary,
)
from crucible_core.schemas.admissions import EventRequest

__all__ = [
    "AdmissionError",
    "AdmissionCoordinator",
    "admit_event",
    "get_event",
    "get_task",
    "list_events",
    "list_tasks",
    "reconcile_incomplete_admissions",
]

logger = get_logger(__name__)

# Temporary injection points for tests. The lifecycle owns the
# canonical workflow; these globals only forward current values.
_capture_baseline = capture_baseline

_RACE_HOOK: Any = None


def admit_event(database_path: Path, event: EventRequest) -> dict[str, object]:
    lifecycle = AdmissionCoordinator(
        database_path,
        capture_baseline=_capture_baseline,
        race_hook=_RACE_HOOK,
    )
    return lifecycle.admit(event)


def reconcile_incomplete_admissions(database_path: Path) -> None:
    lifecycle = AdmissionCoordinator(
        database_path,
        capture_baseline=_capture_baseline,
        race_hook=_RACE_HOOK,
    )
    lifecycle.reconcile_incomplete()


def get_event(database_path: Path, event_id: str) -> dict[str, object] | None:
    with connect(database_path) as connection:
        row = admissions_repo.find_event_detail(connection, event_id)
    if not row:
        return None
    return event_detail(event_id, row).model_dump()


def list_events(
    database_path: Path, limit: int, cursor: str | None
) -> dict[str, object]:
    with connect(database_path) as connection:
        rows = admissions_repo.list_events(connection, limit)
    return {"events": [row.model_dump() for row in rows]}


def list_tasks(
    database_path: Path, limit: int, cursor: str | None
) -> dict[str, object]:
    started_at: str | None = None
    cursor_task_id: str | None = None
    if cursor:
        try:
            started_at, cursor_task_id = json.loads(
                base64.urlsafe_b64decode(cursor).decode()
            )
        except (
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            binascii.Error,
        ):
            logger.warning("task list failed code=INVALID_CURSOR")
            raise AdmissionError("INVALID_CURSOR", 400) from None
        if not (
            isinstance(started_at, str)
            and isinstance(cursor_task_id, str)
            and started_at
            and cursor_task_id
        ):
            raise AdmissionError("INVALID_CURSOR", 400)
    with connect(database_path) as connection:
        rows = tasks_repo.list_tasks_page(
            connection, started_at, cursor_task_id, limit
        )
        task_ids = [row.id for row in rows[:limit]]
        input_rows = tasks_repo.list_inputs_by_task_ids(connection, task_ids)

    inputs: dict[str, list[str]] = {task_id: [] for task_id in task_ids}

    for link in input_rows:
        inputs[link.task_id].append(link.input_id)

    tasks = [
        {**task_summary(row).model_dump(), "input_ids": inputs[row.id]}
        for row in rows[:limit]
    ]
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps([last.started_at, last.id]).encode()
        ).decode()
    return {"tasks": tasks, "next_cursor": next_cursor}


def get_task(database_path: Path, task_id: str) -> dict[str, object] | None:
    with connect(database_path) as connection:
        row = tasks_repo.get_task_row(connection, task_id)
        if not row:
            return None
        inputs = [
            item.input_id
            for item in tasks_repo.list_input_ids_by_task(connection, task_id)
        ]
        files = tasks_repo.list_task_baseline_files(connection, task_id)
    return task_detail(row, inputs, files).model_dump()
