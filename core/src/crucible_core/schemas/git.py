from __future__ import annotations

from dataclasses import dataclass, field

from crucible_core.schemas.persistence import BaselineFileRow


@dataclass(frozen=True)
class IndexEntry:
    """Single versioned index manifest entry (raw path bytes)."""

    path: bytes
    oid: str
    mode: str


@dataclass(frozen=True)
class HashedContent:
    """Chunked hash result with retained payload.

    ``data`` is the gzip-compressed payload (or ``None`` when oversize
    or binary) for worktree hashing, and the raw retained bytes for
    streamed blobs. Callers distinguish via context, never position.
    """

    sha256: str
    size: int
    is_binary: int
    data: bytes | bytearray | None


@dataclass(frozen=True)
class StreamedContent:
    """Streamed hash result plus the final clock reading."""

    content: HashedContent
    finished_at: float


@dataclass(frozen=True)
class BaselineCaptureSnapshot:
    """Typed baseline capture (head/branch/status/index/files)."""

    head: str
    branch: str
    status: bytes
    index: bytes
    files: list[BaselineFileRow] = field(default_factory=list)
