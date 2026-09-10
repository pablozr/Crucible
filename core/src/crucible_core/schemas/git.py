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

    ``data`` encoding depends on the producer and must not be guessed:

    - :func:`hash_worktree_file` returns gzip-compressed snapshot bytes
      (``None`` when oversize or binary), ready to persist as
      ``BaselineFileRow.content`` via ``bytes(data)``.
    - :func:`hash_stream` returns RAW retained bytes (a ``bytearray`` of
      at most ``max_size + 1`` bytes). Convert them with
      ``evidence_content.snapshot_bytes_for_raw``; never pass them to a
      decompressor.

    Capture code must use ``evidence_content`` codecs instead of calling
    ``gzip`` directly so the direction (raw vs snapshot) stays explicit.
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
