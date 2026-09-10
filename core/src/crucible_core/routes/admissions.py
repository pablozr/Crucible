from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from crucible_core.core.errors import FinalizationError, ProblemError
from crucible_core.infrastructure.git import final_capture_worker as worker
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
from crucible_core.schemas.git import BaselineCaptureSnapshot
from crucible_core.services.admissions import (
    AdmissionError,
    admit_event,
    get_event,
    get_task,
    list_tasks,
)
from crucible_core.services.finalizations import abort_event, complete_event
from crucible_core.utils.functions import transport_json_sha256

router = APIRouter()

logger = get_logger(__name__)


async def _capture_transport_hash(request: Request) -> None:
    body = await request.body()
    request.state.transport_hash = (
        transport_json_sha256(body) if body else None
    )


@dataclass(frozen=True)
class Composition:
    """Immutable per-app dependency composition.

    Built once via the app factory and never mutated during
    requests, so concurrent requests and lifecycles cannot observe
    a half-updated injection. ``None`` selects the production
    default for that slot (real baseline capture, no hooks,
    system clocks, lifespan-owned runner).
    """

    capture_runner: worker.CaptureRunner | None = None
    capture_baseline: (
        Callable[[Path, int, float], BaselineCaptureSnapshot] | None
    ) = None
    race_hook: Any | None = None
    publication_hook: Callable[[], None] | None = None
    clock: Callable[[], datetime] | None = None
    monotonic: Callable[[], float] | None = None


DEFAULT_COMPOSITION = Composition()


def _composition(request: Request) -> Composition:
    composition = getattr(request.app.state, "composition", None)
    if composition is None:
        return DEFAULT_COMPOSITION
    return composition


def capture_runner_dep(request: Request) -> worker.CaptureRunner:
    # The lifespan owns the single shared runner: a factory-provided
    # test runner when composed, else a fresh process runner. The
    # default only covers direct app usage without lifespan.
    composed = _composition(request).capture_runner
    if composed is not None:
        return composed
    runner = getattr(request.app.state, "capture_runner", None)
    if runner is None:
        return worker.get_default_runner()
    return runner


def capture_baseline_dep(request: Request):
    """Composed baseline capture (None -> real capture)."""
    return _composition(request).capture_baseline


def race_hook_dep(request: Request):
    """Composed admission race hook (None in production)."""
    return _composition(request).race_hook


def publication_hook_dep(request: Request):
    """Composed finalization publication hook (None in production)."""
    return _composition(request).publication_hook


def clock_dep(request: Request):
    """Composed clock (None -> system utcnow)."""
    return _composition(request).clock


def monotonic_dep(request: Request):
    """Composed monotonic clock (None -> time.monotonic)."""
    return _composition(request).monotonic


@router.post("/events", response_model=ResponseEnvelope[EventData])
def post_event(
    request: Request,
    event: EventRequest,
    _transport: None = Depends(_capture_transport_hash),
    capture_runner: worker.CaptureRunner = Depends(capture_runner_dep),
    capture_baseline=Depends(capture_baseline_dep),
    race_hook=Depends(race_hook_dep),
    publication_hook=Depends(publication_hook_dep),
    clock=Depends(clock_dep),
    monotonic=Depends(monotonic_dep),
) -> ResponseEnvelope[EventData]:
    transport_hash = getattr(request.state, "transport_hash", None)
    try:
        if event.event_type == "input_candidate":
            result = admit_event(
                request.app.state.settings.database_path,
                event,
                transport_hash,
                capture_runner,
                capture_baseline=capture_baseline,
                race_hook=race_hook,
            )
        elif event.event_type == "task_completed":
            result = complete_event(
                request.app.state.settings.database_path,
                event,
                request.app.state.settings.terminal_max_authorization_window_seconds,
                transport_hash,
                capture_runner,
                publication_hook=publication_hook,
                clock=clock,
                monotonic=monotonic,
            )
        elif event.event_type == "task_finalization_aborted":
            result = abort_event(
                request.app.state.settings.database_path,
                event,
                transport_hash,
                capture_runner,
                publication_hook=publication_hook,
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
