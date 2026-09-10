from __future__ import annotations

import gzip
import hashlib

from crucible_core.schemas.persistence import BaselineFileRow

"""Snapshot byte codecs and evidence row builders for Git capture.

Single owner of the persisted-bytes contract used by final capture:

- ``BaselineFileRow.content`` is ALWAYS gzip-compressed snapshot bytes
  (or ``None`` when binary/oversize). It is the only payload that may be
  passed to :func:`decompress_snapshot_bytes`.
- ``hash_stream`` raw retained bytes (``HashedContent.data`` from a
  streamed ``git show``) are NEVER compressed and must never be passed
  to a decompressor. Convert them with :func:`snapshot_bytes_for_raw`
  or hand them to :func:`symlink_row` directly.
- ``hash_worktree_file`` already returns gzip-compressed snapshot bytes
  in ``HashedContent.data``; copy them with ``bytes(...)``, never
  re-compress or decompress them in the capture path.

Keeping both directions here makes it impossible to decompress raw
retained bytes by mistake: fresh HEAD reads keep the raw hash result
and build rows without a compress/decompress round-trip.
"""


def compress_snapshot_bytes(raw: bytes | bytearray) -> bytes:
    """Compress raw bytes into persisted snapshot payload bytes."""
    return gzip.compress(bytes(raw))


def decompress_snapshot_bytes(stored: bytes | bytearray) -> bytes:
    """Decompress persisted snapshot bytes.

    Only ``BaselineFileRow.content`` (non-``None``) may be passed here.
    Raises ``OSError`` on non-gzip input so callers fail closed.
    """
    return gzip.decompress(bytes(stored))


def snapshot_bytes_for_raw(
    *,
    raw: bytes | bytearray,
    is_binary: int,
    size: int,
    max_size: int,
) -> bytes | None:
    """Build the persisted snapshot payload for raw retained bytes.

    Single binary/size/compress policy for streamed Git blobs: binary
    or oversize content stores ``None`` (hash/metadata only), otherwise
    gzip-compressed snapshot bytes.
    """
    if is_binary or size > max_size:
        return None
    return gzip.compress(bytes(raw))


def symlink_row(
    path: str, status: str, link_bytes: bytes | bytearray, max_size: int
) -> BaselineFileRow:
    """Build structural evidence for a symlink without following it.

    ``link_bytes`` are the RAW link-target bytes (from ``git show`` of a
    ``120000`` blob, ``os.readlink``, or the staged blob); this is the
    only place that hashes/compresses them. Structural mode ``120000``
    is persisted; destination content is never hashed.
    """
    raw = bytes(link_bytes)
    digest = hashlib.sha256(raw).hexdigest()
    saved = None
    if len(raw) <= max_size:
        saved = gzip.compress(raw)
    return BaselineFileRow(
        path=path,
        status=status,
        sha256=digest,
        size=len(raw),
        is_binary=0,
        content=saved,
        mode="120000",
        gitlink_oid=None,
    )


def gitlink_row(
    path: str, status: str, *, mode: str, oid: str | None
) -> BaselineFileRow:
    """Build structural evidence for a gitlink (submodule pointer).

    Identity is mode + commit OID; internal content is never snapshotted.
    """
    return BaselineFileRow(
        path=path,
        status=status,
        sha256=None,
        size=None,
        is_binary=None,
        content=None,
        mode=mode,
        gitlink_oid=oid,
    )


def regular_blob_row(
    path: str,
    status: str,
    *,
    sha256: str | None,
    size: int | None,
    is_binary: int | None,
    content: bytes | None,
    mode: str | None,
) -> BaselineFileRow:
    """Build evidence for a regular blob from an explicit snapshot."""
    return BaselineFileRow(
        path=path,
        status=status,
        sha256=sha256,
        size=size,
        is_binary=is_binary,
        content=content,
        mode=mode,
        gitlink_oid=None,
    )


def structural_row(
    path: str,
    status: str,
    *,
    mode: str | None,
    gitlink_oid: str | None = None,
) -> BaselineFileRow:
    """Build content-less evidence for special files (no hash/snapshot)."""
    return BaselineFileRow(
        path=path,
        status=status,
        sha256=None,
        size=None,
        is_binary=None,
        content=None,
        mode=mode,
        gitlink_oid=gitlink_oid,
    )
