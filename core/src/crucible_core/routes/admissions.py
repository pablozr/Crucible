from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request

from crucible_core.core.errors import FinalizationError, ProblemError
from crucible_core.logging import get_logger
from crucible_core.schemas.admissions import (
    EventDetail,
    EventRequest,
    EventResponse,
    TaskDetail,
    TaskList,
)
from crucible_core.schemas.envelope import (
    EventData,
    EventDetailData,
    ResponseEnvelope,
    TaskData,
    TasksData,
)
from crucible_core.services.admissions import (
    AdmissionError,
    admit_event,
    get_event,
    get_task,
    list_tasks,
)
from crucible_core.services.finalizations import complete_event

router = APIRouter()

logger = get_logger(__name__)


@router.post("/events", response_model=ResponseEnvelope[EventData])
def post_event(
    request: Request, event: EventRequest
) -> ResponseEnvelope[EventData]:
    try:
        if event.event_type == "input_candidate":
            result = admit_event(
                request.app.state.settings.database_path, event
            )
        elif event.event_type == "task_completed":
            result = complete_event(
                request.app.state.settings.database_path, event
            )
        else:
            raise AdmissionError("UNKNOWN_EVENT_TYPE")
    except (AdmissionError, FinalizationError) as error:
        logger.warning(
            "admission error code=%s status=%s event_id=%s",
            error.code,
            error.status_code,
            event.event_id,
        )
        raise ProblemError(error.code, error.status_code) from error
    body = EventResponse.model_validate(result)
    return ResponseEnvelope(
        status="ok",
        message="Event received.",
        data=EventData(event=body),
    )


@router.get(
    "/events/{event_id}", response_model=ResponseEnvelope[EventDetailData]
)
def event(
    request: Request, event_id: UUID
) -> ResponseEnvelope[EventDetailData]:
    result = get_event(request.app.state.settings.database_path, str(event_id))
    if not result:
        raise ProblemError("EVENT_NOT_FOUND", 404)
    body = EventDetail.model_validate(result)
    return ResponseEnvelope(
        status="ok",
        message="Event retrieved.",
        data=EventDetailData(event=body),
    )


@router.get("/tasks", response_model=ResponseEnvelope[TasksData])
def tasks(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
) -> ResponseEnvelope[TasksData]:
    try:
        result = list_tasks(
            request.app.state.settings.database_path, limit, cursor
        )
    except AdmissionError as error:
        logger.warning(
            "tasks list failed code=%s status=%s",
            error.code,
            error.status_code,
        )
        raise ProblemError(error.code, error.status_code) from error
    body = TaskList.model_validate(result)
    return ResponseEnvelope(
        status="ok",
        message="Tasks retrieved.",
        data=TasksData(tasks=body.tasks, next_cursor=body.next_cursor),
    )


@router.get("/tasks/{task_id}", response_model=ResponseEnvelope[TaskData])
def task(request: Request, task_id: UUID) -> ResponseEnvelope[TaskData]:
    result = get_task(request.app.state.settings.database_path, str(task_id))
    if not result:
        raise ProblemError("TASK_NOT_FOUND", 404)
    body = TaskDetail.model_validate(result)
    return ResponseEnvelope(
        status="ok",
        message="Task retrieved.",
        data=TaskData(task=body),
    )
