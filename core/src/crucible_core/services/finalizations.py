from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from crucible_core.application.finalizations import FinalizationCoordinator
from crucible_core.infrastructure.git import final_capture_worker as worker
from crucible_core.schemas.admissions import EventRequest


def _resolve_runner(
    capture_runner: worker.CaptureRunner | None,
) -> worker.CaptureRunner:
    # Production composition (routes/lifespan) always injects the
    # shared runner explicitly; the default only covers direct calls.
    return capture_runner or worker.get_default_runner()


def complete_event(
    database_path: Path,
    event: EventRequest,
    max_authorization_window_seconds: int | None = None,
    payload_hash: str | None = None,
    capture_runner: worker.CaptureRunner | None = None,
    *,
    publication_hook: Callable[[], None] | None = None,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> dict[str, object]:
    resolved_clock = clock or (lambda: datetime.now(UTC))
    return FinalizationCoordinator(
        database_path,
        capture_runner=_resolve_runner(capture_runner),
        publication_hook=publication_hook,
        clock=resolved_clock,
        max_authorization_window_seconds=max_authorization_window_seconds,
        monotonic=monotonic,
    ).complete(event, payload_hash)


def abort_event(
    database_path: Path,
    event: EventRequest,
    payload_hash: str | None = None,
    capture_runner: worker.CaptureRunner | None = None,
    *,
    publication_hook: Callable[[], None] | None = None,
) -> dict[str, object]:
    return FinalizationCoordinator(
        database_path,
        capture_runner=_resolve_runner(capture_runner),
        publication_hook=publication_hook,
    ).abort(event, payload_hash)


def recover_finalizations(
    database_path: Path,
    capture_runner: worker.CaptureRunner | None = None,
) -> None:
    FinalizationCoordinator(
        database_path, capture_runner=_resolve_runner(capture_runner)
    ).recover()


def fence_unfrozen_finalization(
    database_path: Path,
    tree_id: str,
    capture_runner: worker.CaptureRunner | None = None,
) -> int:
    return FinalizationCoordinator(
        database_path, capture_runner=_resolve_runner(capture_runner)
    ).fence_unfrozen(tree_id)
