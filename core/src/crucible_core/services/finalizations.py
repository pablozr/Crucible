from __future__ import annotations

from pathlib import Path
from typing import Any

from crucible_core.application.finalizations import FinalizationCoordinator
from crucible_core.infrastructure.git.final_capture import capture_final
from crucible_core.schemas.admissions import EventRequest

_capture_final = capture_final
_PUBLICATION_HOOK: Any = None


def complete_event(
    database_path: Path, event: EventRequest
) -> dict[str, object]:
    return FinalizationCoordinator(
        database_path,
        capture_final=_capture_final,
        publication_hook=_PUBLICATION_HOOK,
    ).complete(event)


def recover_finalizations(database_path: Path) -> None:
    FinalizationCoordinator(
        database_path, capture_final=_capture_final
    ).recover()


def fence_unfrozen_finalization(database_path: Path, tree_id: str) -> int:
    return FinalizationCoordinator(
        database_path, capture_final=_capture_final
    ).fence_unfrozen(tree_id)
