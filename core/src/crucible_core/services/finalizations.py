from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from crucible_core.application.finalizations import FinalizationCoordinator
from crucible_core.infrastructure.git.final_capture import capture_final
from crucible_core.schemas.admissions import EventRequest

_capture_final = capture_final
_PUBLICATION_HOOK: Any = None
_CLOCK: Callable[[], datetime] | None = None
_MONOTONIC: Callable[[], float] | None = None
_MAX_AUTHORIZATION_WINDOW_SECONDS: int | None = None


def complete_event(
    database_path: Path,
    event: EventRequest,
    max_authorization_window_seconds: int | None = None,
) -> dict[str, object]:
    clock = _CLOCK or (lambda: datetime.now(UTC))
    window = max_authorization_window_seconds
    if window is None:
        window = _MAX_AUTHORIZATION_WINDOW_SECONDS
    return FinalizationCoordinator(
        database_path,
        capture_final=_capture_final,
        publication_hook=_PUBLICATION_HOOK,
        clock=clock,
        max_authorization_window_seconds=window,
        monotonic=_MONOTONIC,
    ).complete(event)


def abort_event(database_path: Path, event: EventRequest) -> dict[str, object]:
    return FinalizationCoordinator(
        database_path,
        capture_final=_capture_final,
        publication_hook=_PUBLICATION_HOOK,
    ).abort(event)


def recover_finalizations(database_path: Path) -> None:
    FinalizationCoordinator(
        database_path, capture_final=_capture_final
    ).recover()


def fence_unfrozen_finalization(database_path: Path, tree_id: str) -> int:
    return FinalizationCoordinator(
        database_path, capture_final=_capture_final
    ).fence_unfrozen(tree_id)
