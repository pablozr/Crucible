from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from crucible_core.schemas.persistence import (
    BaselineFileRow,
    FinalizationTask,
    TaskFileChangeRow,
)


@dataclass(frozen=True)
class FinalCaptureRequest:
    """Git-only input for the isolated final-capture child process."""

    git_root: str
    baseline_head: str
    baseline_branch: str
    baseline_index: bytes
    baseline_files: list[BaselineFileRow] = field(default_factory=list)
    max_file_size_bytes: int = 0
    deadline_monotonic: float = 0.0


@dataclass(frozen=True)
class FinalCaptureSnapshot:
    """Typed snapshot returned by the child over IPC (bytes included)."""

    head: str
    branch: str
    status: bytes
    index: bytes
    baseline_files: list[BaselineFileRow] = field(default_factory=list)
    changes: list[TaskFileChangeRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "head": self.head,
            "branch": self.branch,
            "status": self.status,
            "index": self.index,
            "baseline_files": list(self.baseline_files),
            "changes": list(self.changes),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FinalCaptureSnapshot:
        return cls(
            head=str(payload["head"]),
            branch=str(payload["branch"]),
            status=bytes(payload["status"]),
            index=bytes(payload["index"]),
            baseline_files=list(payload.get("baseline_files") or []),
            changes=list(payload.get("changes") or []),
        )


@dataclass(frozen=True)
class BeginFinalizationResult:
    """Named result of ``_begin`` (no positional tuple)."""

    generation: int
    task: FinalizationTask
    input_row_id: str


@dataclass(frozen=True)
class BeginReplayed:
    """Explicit replay branch for ``_begin`` (no bare dict union)."""

    response: dict[str, object]


@dataclass(frozen=True)
class FinalCaptureEnvelope:
    """IPC envelope: success carries a snapshot, failure carries a code."""

    ok: bool
    snapshot: FinalCaptureSnapshot | None = None
    error_code: str | None = None
    error_status: int = 400
