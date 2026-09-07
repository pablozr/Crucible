from __future__ import annotations

import base64

from crucible_core.schemas.admissions import (
    BaselineFile,
    EventDetail,
    EventResponse,
    TaskDetail,
    TaskFileChange,
    TaskSummary,
)
from crucible_core.schemas.persistence import (
    BaselineFileRow,
    InboundEvent,
    InboundEventDetail,
    TaskDetailRow,
    TaskFileChangeRow,
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
        dispatch_authorized=outcome == "admitted",
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
        dispatch_authorized=event.outcome == "admitted",
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
    changes: list[TaskFileChangeRow],
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
        final_head=row.final_head,
        final_branch=row.final_branch,
        final_status=base64.b64encode(row.final_status).decode()
        if row.final_status
        else None,
        final_index_manifest=(
            base64.b64encode(row.final_index_manifest).decode()
            if row.final_index_manifest
            else None
        ),
        snapshot_frozen_at=row.snapshot_frozen_at,
        task_diff=row.task_diff,
        evidence_completeness=row.evidence_completeness,
        execution_id=row.execution_id,
        terminal_signal=row.terminal_signal,
        terminal_outcome=row.terminal_outcome,
        compatibility_profile=row.compatibility_profile,
        terminal_observed_at=row.terminal_observed_at,
        capture_not_after=row.capture_not_after,
        file_changes=[
            TaskFileChange(
                path=item.path,
                operation=item.operation,
                final_status=item.final_status,
                final_sha256=item.final_sha256,
                final_size=item.final_size,
                final_is_binary=bool(item.final_is_binary)
                if item.final_is_binary is not None
                else None,
                evidence_status=item.evidence_status,
                evidence_reason=item.evidence_reason,
                patch=item.patch,
            )
            for item in changes
        ],
    )
