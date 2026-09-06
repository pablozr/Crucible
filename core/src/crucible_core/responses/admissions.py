from __future__ import annotations

import base64

from crucible_core.schemas.admissions import (
    BaselineFile,
    EventDetail,
    EventResponse,
    TaskDetail,
    TaskSummary,
)
from crucible_core.schemas.persistence import (
    BaselineFileRow,
    InboundEvent,
    InboundEventDetail,
    TaskDetailRow,
    TaskPageRow,
)


def event_response(
    event_id: str,
    status: str,
    outcome: str,
    input_id: str | None,
    task_id: str | None,
) -> EventResponse:
    return EventResponse(
        event_id=event_id,
        status=status,
        outcome=outcome,
        input_id=input_id,
        task_id=task_id,
        dispatch_authorized=status == "accepted",
    )


def event_detail(
    event_id: str,
    event: InboundEventDetail,
) -> EventDetail:
    return EventDetail(
        event_id=event_id,
        status=event.status,
        outcome=event.outcome,
        input_id=event.input_id,
        task_id=event.task_id,
        dispatch_authorized=event.status == "accepted",
        payload_hash=event.payload_hash,
        failure_code=event.failure_code,
    )


def reconciled_event_response(
    event_id: str, event: InboundEvent
) -> EventResponse:
    return event_response(
        event_id,
        event.status,
        event.outcome,
        event.input_id,
        event.task_id,
    )


def task_summary(row: TaskPageRow) -> TaskSummary:
    return TaskSummary(
        id=row.id,
        status=row.status,
        started_at=row.started_at,
        worktree=row.worktree,
        project_id=row.project_id,
        branch=row.branch,
        failure_code=row.failure_code,
        failure_message=row.failure_message,
    )


def task_detail(
    row: TaskDetailRow,
    input_ids: list[str],
    files: list[BaselineFileRow],
) -> TaskDetail:
    return TaskDetail(
        id=row.id,
        status=row.status,
        started_at=row.started_at,
        worktree=row.worktree,
        project_id=row.project_id,
        branch=row.branch,
        failure_code=row.failure_code,
        failure_message=row.failure_message,
        baseline_head=row.baseline_head,
        baseline_status=base64.b64encode(row.baseline_status).decode()
        if row.baseline_status
        else None,
        baseline_index_manifest=(
            base64.b64encode(row.baseline_index_manifest).decode()
            if row.baseline_index_manifest
            else None
        ),
        input_ids=input_ids,
        baseline_files=[
            BaselineFile(
                path=item.path,
                status=item.status,
                sha256=item.sha256,
                size=item.size,
                is_binary=bool(item.is_binary)
                if item.is_binary is not None
                else None,
                content=base64.b64encode(item.content).decode()
                if item.content
                else None,
            )
            for item in files
        ],
    )
