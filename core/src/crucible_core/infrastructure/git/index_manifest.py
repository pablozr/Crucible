from __future__ import annotations

import base64
import binascii
import hashlib
import json

from crucible_core.schemas.git import IndexEntry

INDEX_MANIFEST_VERSION = 1

_ALLOWED_MODES = frozenset({"100644", "100755", "120000", "160000"})


def parse_ls_files(raw: bytes) -> list[IndexEntry]:
    """Parse `git ls-files -s -z` bytes into ordered index entries.

    Paths are kept as raw bytes so non-UTF-8 Git paths round-trip
    losslessly via base64. Only fully-merged (stage 0) entries are
    accepted; anything else fails closed for the caller to map to a
    safe capture error.
    """
    entries: list[IndexEntry] = []
    if not raw:
        return entries
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, separator, path = record.partition(b"\t")
        if not separator or not path:
            raise ValueError("malformed index entry")
        try:
            text = header.decode("ascii")
        except UnicodeDecodeError:
            raise ValueError("malformed index header") from None
        parts = text.split(" ")
        if len(parts) != 3:
            raise ValueError("malformed index header")
        mode, oid, stage = parts
        if mode not in _ALLOWED_MODES:
            raise ValueError("unsupported index mode")
        if len(oid) not in (40, 64) or any(
            character not in "0123456789abcdef" for character in oid
        ):
            raise ValueError("malformed index oid")
        if stage != "0":
            raise ValueError("unmerged index stage")
        entries.append(IndexEntry(path=path, oid=oid, mode=mode))
    return entries


def serialize_manifest(
    entries: list[IndexEntry],
) -> bytes:
    """Serialize entries to canonical versioned JSON bytes.

    Semantically an ordered collection of {path_b64, oid, mode};
    ordering is deterministic by raw path bytes.
    """
    ordered = sorted(entries, key=lambda item: item.path)
    payload = {
        "entries": [
            {
                "mode": entry.mode,
                "oid": entry.oid,
                "path_b64": base64.b64encode(entry.path).decode("ascii"),
            }
            for entry in ordered
        ],
        "version": INDEX_MANIFEST_VERSION,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def build_canonical_manifest(raw: bytes) -> bytes:
    """Canonicalize raw `git ls-files -s -z` output."""
    return serialize_manifest(parse_ls_files(raw))


def manifest_sha256(canonical: bytes) -> str:
    return hashlib.sha256(canonical).hexdigest()


def parse_canonical_manifest(
    data: bytes,
) -> list[IndexEntry]:
    """Parse and validate canonical manifest bytes."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("malformed index manifest") from None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != INDEX_MANIFEST_VERSION
    ):
        raise ValueError("unsupported index manifest version")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("malformed index manifest entries")
    entries: list[IndexEntry] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            raise ValueError("malformed index manifest entry")
        mode = item.get("mode")
        oid = item.get("oid")
        path_b64 = item.get("path_b64")
        if (
            not isinstance(mode, str)
            or not isinstance(oid, str)
            or not isinstance(path_b64, str)
        ):
            raise ValueError("malformed index manifest entry")
        if mode not in _ALLOWED_MODES:
            raise ValueError("unsupported index mode")
        if len(oid) not in (40, 64) or any(
            character not in "0123456789abcdef" for character in oid
        ):
            raise ValueError("malformed index oid")
        try:
            path = base64.b64decode(path_b64.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error):
            raise ValueError("malformed index path") from None
        entries.append(IndexEntry(path=path, oid=oid, mode=mode))
    if [entry.path for entry in entries] != sorted(
        [entry.path for entry in entries]
    ):
        raise ValueError("non-deterministic index order")
    return entries


def normalize_stored_manifest(stored: bytes | None) -> bytes | None:
    """Return canonical bytes for stored manifests.

    Accepts canonical JSON (re-serialized to enforce determinism) and
    legacy raw `git ls-files -s -z` bytes. Returns None when there is
    nothing stored; raises ValueError when stored bytes are corrupt.
    """
    if stored is None:
        return None
    if not stored:
        return serialize_manifest([])
    try:
        entries = parse_canonical_manifest(bytes(stored))
    except ValueError:
        entries = parse_ls_files(bytes(stored))
    return serialize_manifest(entries)


def sha256_for_stored(stored: bytes | None) -> str | None:
    """Derive canonical SHA-256 for stored manifest bytes, fail-safe."""
    try:
        canonical = normalize_stored_manifest(stored)
    except ValueError:
        return None
    if canonical is None:
        return None
    return manifest_sha256(canonical)
