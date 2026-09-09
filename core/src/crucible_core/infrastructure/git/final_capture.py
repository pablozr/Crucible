from __future__ import annotations

import gzip
import hashlib
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from crucible_core.core.errors import FinalizationError
from crucible_core.infrastructure.git.content_hash import (
    HashBudget,
    hash_stream,
    hash_worktree_file,
)
from crucible_core.infrastructure.git.index_manifest import (
    build_canonical_manifest,
)
from crucible_core.schemas.finalizations import FinalCaptureSnapshot
from crucible_core.schemas.git import HashedContent
from crucible_core.schemas.persistence import (
    BaselineFileRow,
    TaskFileChangeRow,
)

SUBPROCESS_TIMEOUT_SECONDS = 2
HASH_PER_FILE_BUDGET_SECONDS = 2.0
HASH_AGGREGATE_BUDGET_SECONDS = 3.0

EVIDENCE_COMPLETE = "complete"
EVIDENCE_HASH_ONLY = "hash_only"
EVIDENCE_UNSUPPORTED = "unsupported"
EVIDENCE_UNAVAILABLE = "unavailable"

REASON_BINARY_CONTENT = "BINARY_CONTENT"
REASON_SIZE_LIMIT = "SNAPSHOT_SIZE_LIMIT"
REASON_SYMLINK = "SYMLINK_TARGET"
REASON_GITLINK = "GITLINK_CONTENT"
REASON_MODE_ONLY = "MODE_ONLY_CHANGE"
REASON_SPECIAL_FILE = "SPECIAL_FILE_TYPE"

_ALLOWED_PARTIAL_STATUSES = frozenset(
    {EVIDENCE_COMPLETE, EVIDENCE_HASH_ONLY, EVIDENCE_UNSUPPORTED}
)


@dataclass
class _PathEvidence:
    baseline_files: list[BaselineFileRow] = field(default_factory=list)
    changes: list[TaskFileChangeRow] = field(default_factory=list)


@dataclass(frozen=True)
class _TreeEntry:
    mode: str
    kind: str
    oid: str | None = None


@dataclass(frozen=True)
class _IndexEntry:
    mode: str
    oid: str
    stage: str


def capture_final(
    root: Path,
    baseline_head: str,
    baseline_branch: str,
    baseline_index: bytes,
    baseline_files: list[BaselineFileRow],
    max_size: int,
    deadline: float,
    *,
    clock: Callable[[], float] | None = None,
    opener: Callable[..., Any] | None = None,
) -> FinalCaptureSnapshot:
    # baseline_index is retained for signature compatibility; index
    # changes are allowed in slice 7.2 and final index is evidence only.
    _ = baseline_index
    tick = clock or time.monotonic
    open_fn = opener or open
    budget = HashBudget(
        aggregate_budget=HASH_AGGREGATE_BUDGET_SECONDS,
        per_file_budget=HASH_PER_FILE_BUDGET_SECONDS,
    )
    unstable: FinalizationError | None = None
    for _ in range(2):
        try:
            first = _git_state(root, deadline, tick)
            _validate_supported_state(
                first, baseline_head, baseline_branch, root, deadline, tick
            )
            evidence = _capture_paths(
                root,
                baseline_head,
                first.head,
                first.status,
                baseline_files,
                max_size,
                deadline,
                tick,
                open_fn,
                budget,
            )
            second = _git_state(root, deadline, tick)
            _validate_supported_state(
                second, baseline_head, baseline_branch, root, deadline, tick
            )
            second_evidence = _capture_paths(
                root,
                baseline_head,
                second.head,
                second.status,
                baseline_files,
                max_size,
                deadline,
                tick,
                open_fn,
                budget,
            )
        except FinalizationError as error:
            if error.code != "FINAL_SNAPSHOT_UNSTABLE":
                raise
            unstable = error
            continue
        if first != second or _evidence_identity(
            evidence
        ) != _evidence_identity(second_evidence):
            unstable = FinalizationError("FINAL_SNAPSHOT_UNSTABLE")
            continue

        return FinalCaptureSnapshot(
            head=first.head,
            branch=first.branch,
            status=first.status,
            index=first.index,
            baseline_files=list(evidence.baseline_files),
            changes=list(evidence.changes),
        )
    raise (
        unstable
        if unstable is not None
        else FinalizationError("FINAL_SNAPSHOT_UNSTABLE")
    )


def _validate_supported_state(
    state: FinalCaptureSnapshot,
    baseline_head: str,
    baseline_branch: str,
    root: Path,
    deadline: float,
    tick: Callable[[], float],
) -> None:
    if state.branch != baseline_branch:
        raise FinalizationError("BRANCH_CHANGED_DURING_TASK")
    if state.head != baseline_head:
        _ensure_same_branch_advance(
            root, baseline_head, state.head, deadline, tick
        )


def _ensure_same_branch_advance(
    root: Path,
    baseline_head: str,
    final_head: str,
    deadline: float,
    tick: Callable[[], float],
) -> None:
    timeout = min(SUBPROCESS_TIMEOUT_SECONDS, deadline - tick())
    if timeout <= 0:
        raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "merge-base",
                "--is-ancestor",
                baseline_head,
                final_head,
            ],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT") from error
    except OSError as error:
        if tick() >= deadline:
            raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT") from error
        raise FinalizationError("FINAL_CAPTURE_FAILED") from error
    if tick() >= deadline:
        raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
    if result.returncode == 0:
        return
    if result.returncode == 1:
        raise FinalizationError("UNSUPPORTED_HEAD_STATE")
    raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE")


_REGULAR_BLOB_MODES = frozenset({"100644", "100755"})
_CLEAN_FINAL_STATUS = "  "


def _head_changed_paths(
    root: Path,
    baseline_head: str,
    final_head: str,
    deadline: float,
    tick: Callable[[], float],
) -> set[str]:
    if baseline_head == final_head:
        return set()
    try:
        raw = _git(
            root,
            [
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                baseline_head,
                final_head,
            ],
            deadline,
            tick,
        )
    except FinalizationError as error:
        if error.code == "FINAL_CAPTURE_FAILED":
            raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from error
        raise
    paths: set[str] = set()
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        try:
            paths.add(chunk.decode("utf-8"))
        except UnicodeDecodeError:
            raise FinalizationError("UNSUPPORTED_FINAL_PATH") from None
    return paths


def _git_state(
    root: Path, deadline: float, tick: Callable[[], float]
) -> FinalCaptureSnapshot:
    try:
        head = _git(root, ["rev-parse", "--verify", "HEAD"], deadline, tick)
        branch = _git(
            root,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            deadline,
            tick,
        )
    except FinalizationError as error:
        if error.code == "FINAL_CAPTURE_FAILED":
            raise FinalizationError("UNSUPPORTED_HEAD_STATE") from error
        raise
    if not head or not branch:
        raise FinalizationError("UNSUPPORTED_HEAD_STATE")
    try:
        raw_index = _git(root, ["ls-files", "-s", "-z"], deadline, tick)
        index = build_canonical_manifest(raw_index)
    except ValueError:
        raise FinalizationError("FINAL_CAPTURE_FAILED") from None
    return FinalCaptureSnapshot(
        head=head.decode().strip(),
        branch=branch.decode().strip(),
        status=_git(
            root,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            deadline,
            tick,
        ),
        index=index,
    )


def _capture_paths(
    root: Path,
    baseline_head: str,
    final_head: str,
    final_status: bytes,
    baseline_files: list[BaselineFileRow],
    max_size: int,
    deadline: float,
    tick: Callable[[], float],
    open_fn: Callable[..., Any],
    budget: HashBudget,
) -> _PathEvidence:
    initial_rows = {row.path: row for row in baseline_files}
    final_states = _parse_status(final_status)
    committed = _head_changed_paths(
        root, baseline_head, final_head, deadline, tick
    )
    # Live index map (one bulk `git ls-files -s -z` per stability read).
    # Source for staged effective mode/OID; never the stored manifest.
    index_map = _ls_files_map(root, deadline, tick)
    paths = sorted(set(initial_rows) | set(final_states) | committed)
    frozen_baselines: list[BaselineFileRow] = []
    changes: list[TaskFileChangeRow] = []

    for path in paths:
        _reject_lossy_path(path)
        stored_initial = initial_rows.get(path)
        initial = stored_initial
        if initial is not None and "D" in initial.status:
            initial = None
        initial_entry = _ls_tree_entry(
            root, baseline_head, path, deadline, tick
        )
        final_entry = _ls_tree_entry(root, final_head, path, deadline, tick)
        index_entry = index_map.get(path)
        if stored_initial is None:
            initial = _freeze_baseline_entry(
                root,
                baseline_head,
                path,
                initial_entry,
                max_size,
                deadline,
                tick,
                budget,
            )
            if initial is not None:
                frozen_baselines.append(initial)
        final, structural_reason = _resolve_final(
            root,
            final_head,
            path,
            final_entry,
            index_entry,
            final_states.get(path),
            max_size,
            deadline,
            tick,
            open_fn,
            budget,
        )
        xy = final_states.get(path)
        if _same_file(initial, final) and not _is_mode_only_change(
            initial, final, initial_entry, final_entry, final_states.get(path)
        ):
            if structural_reason is None:
                continue
            # Structural worktree kind (symlink/gitlink/special) with
            # identical hashes still changes the file type: keep it.
            if _entry_kind(initial_entry) == _worktree_kind(
                root, path, final_states.get(path), final_entry
            ):
                continue
        if structural_reason is None and _is_mode_only_change(
            initial, final, initial_entry, final_entry, final_states.get(path)
        ):
            structural_reason = REASON_MODE_ONLY
        # Gitlink OID advance surfaces even when modes match: _same_file
        # already splits on OID, but ensure honest reason when both are
        # gitlinks with different pointers and no other structural flag.
        if structural_reason is None:
            initial_oid = (
                initial.gitlink_oid
                if initial is not None
                else (
                    initial_entry.oid
                    if _is_gitlink_entry(initial_entry)
                    else None
                )
            )
            final_oid = (
                final.gitlink_oid
                if final is not None
                else (
                    final_entry.oid
                    if _is_gitlink_entry(final_entry)
                    else (
                        index_entry.oid
                        if index_entry is not None
                        and index_entry.mode == "160000"
                        else None
                    )
                )
            )
            if (
                initial is not None
                and final is not None
                and initial_oid is not None
                and final_oid is not None
                and initial_oid != final_oid
            ):
                structural_reason = REASON_GITLINK
        if structural_reason is None:
            initial_struct = _reason_for_entry(initial_entry)
            if initial_struct is not None and (
                final is None
                or _entry_kind(initial_entry) != _entry_kind(final_entry)
            ):
                # Deletion or typechange touching a structural baseline
                # (symlink/gitlink/special) stays honest partial.
                structural_reason = initial_struct

        operation = "modified"
        if initial is None:
            operation = "added"
        elif final is None:
            operation = "deleted"
        evidence_status, evidence_reason = _evidence_for_change(
            initial, final, structural_reason
        )
        if evidence_status == EVIDENCE_UNAVAILABLE:
            raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from None
        baseline_mode = (
            initial.mode
            if initial is not None and initial.mode is not None
            else (initial_entry.mode if initial_entry is not None else None)
        )
        baseline_oid = (
            initial.gitlink_oid
            if initial is not None and initial.gitlink_oid is not None
            else (
                initial_entry.oid if _is_gitlink_entry(initial_entry) else None
            )
        )
        # Effective final identity comes only from the resolved final
        # row. A deleted/absent worktree (final None) persists None for
        # final_mode/final_gitlink_oid -- never HEAD/index fallback --
        # while baseline structural metadata is preserved above.
        if final is not None:
            persisted_final_mode = final.mode
            persisted_final_oid = final.gitlink_oid
            if persisted_final_mode is None and final_entry is not None:
                persisted_final_mode = final_entry.mode
            if xy is not None and index_entry is not None:
                x, y = xy[0], xy[1]
                if y == " " and x not in (" ", "?", "!"):
                    persisted_final_mode = index_entry.mode
                    if index_entry.mode == "160000":
                        persisted_final_oid = index_entry.oid
            if persisted_final_oid is None and _is_gitlink_entry(final_entry):
                persisted_final_oid = final_entry.oid
        else:
            persisted_final_mode = None
            persisted_final_oid = None
        changes.append(
            TaskFileChangeRow(
                path=path,
                operation=operation,
                final_status=final_states.get(path, "  "),
                final_sha256=final.sha256 if final else None,
                final_size=final.size if final else None,
                final_is_binary=final.is_binary if final else None,
                final_content=final.content if final else None,
                evidence_status=evidence_status,
                evidence_reason=evidence_reason,
                baseline_mode=baseline_mode,
                baseline_gitlink_oid=baseline_oid,
                final_mode=persisted_final_mode,
                final_gitlink_oid=persisted_final_oid,
            )
        )
    return _PathEvidence(baseline_files=frozen_baselines, changes=changes)


def _parse_status(status: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    # Porcelain v1 -z: records split by NUL; staged renames/copies emit
    # two fields (new-path record with XY, then old path without XY).
    # Rename/copy has no heuristic authority: surface as delete+add.
    records = status.split(b"\0")
    index = 0
    while index < len(records):
        entry = records[index]
        index += 1
        if not entry:
            continue
        try:
            xy = entry[:2].decode("ascii")
        except UnicodeDecodeError:
            raise FinalizationError("UNSUPPORTED_FINAL_PATH") from None
        if len(entry) < 4:
            raise FinalizationError("FINAL_CAPTURE_FAILED") from None
        if "R" in xy or "C" in xy:
            try:
                new_path = entry[3:].decode("utf-8")
            except UnicodeDecodeError:
                raise FinalizationError("UNSUPPORTED_FINAL_PATH") from None
            if index >= len(records) or not records[index]:
                raise FinalizationError("FINAL_CAPTURE_FAILED") from None
            try:
                old_path = records[index].decode("utf-8")
            except UnicodeDecodeError:
                raise FinalizationError("UNSUPPORTED_FINAL_PATH") from None
            index += 1
            _reject_lossy_path(new_path)
            _reject_lossy_path(old_path)
            # Ignored entries never appear without --ignored; keep guard.
            if xy == "!!":
                continue
            if "U" in xy or xy in ("DD", "AA"):
                raise FinalizationError("UNSUPPORTED_FINAL_PATH") from None
            # Delete + add without rename heuristics.
            if old_path not in result:
                result[old_path] = "D "
            result[new_path] = xy
            continue
        if xy == "!!":
            # Ignored paths stay out of scope.
            try:
                ignored_path = entry[3:].decode("utf-8")
            except UnicodeDecodeError:
                raise FinalizationError("UNSUPPORTED_FINAL_PATH") from None
            _reject_lossy_path(ignored_path)
            continue
        if "U" in xy or xy in ("DD", "AA"):
            raise FinalizationError("UNSUPPORTED_FINAL_PATH") from None
        try:
            path = entry[3:].decode("utf-8")
        except UnicodeDecodeError:
            raise FinalizationError("UNSUPPORTED_FINAL_PATH") from None
        _reject_lossy_path(path)
        result[path] = xy
    return result


def _reject_lossy_path(path: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in path):
        raise FinalizationError("UNSUPPORTED_FINAL_PATH") from None


def _evidence_identity(
    evidence: _PathEvidence,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    baselines = [
        (
            row.path,
            row.status,
            row.sha256,
            row.size,
            row.mode,
            row.gitlink_oid,
        )
        for row in evidence.baseline_files
    ]
    changes = [
        (
            row.path,
            row.operation,
            row.final_status,
            row.final_sha256,
            row.final_size,
            row.evidence_status,
            row.evidence_reason,
            row.baseline_mode,
            row.baseline_gitlink_oid,
            row.final_mode,
            row.final_gitlink_oid,
        )
        for row in evidence.changes
    ]
    return baselines, changes


def _read_head_file(
    root: Path,
    head: str,
    path: str,
    max_size: int,
    deadline: float,
    tick: Callable[[], float],
    budget: HashBudget,
) -> BaselineFileRow | None:
    entry = _ls_tree_entry(root, head, path, deadline, tick)
    if entry is None or entry.kind == "tree":
        return None
    if _is_gitlink_entry(entry):
        return _gitlink_row(path, "  ", entry)
    # Regular blobs and symlink targets stream via ``git show``; symlink
    # link bytes are preserved by the caller as structural evidence.
    streamed = _stream_git_blob_to_row(
        root, head, path, "  ", max_size, deadline, tick, budget
    )
    if _is_symlink_entry(entry):
        link_bytes: bytes | None = None
        if streamed.content is not None:
            try:
                link_bytes = gzip.decompress(bytes(streamed.content))
            except OSError:
                link_bytes = None
        if link_bytes is not None:
            return _symlink_row(path, "  ", link_bytes, max_size)
        return BaselineFileRow(
            path=streamed.path,
            status="  ",
            sha256=streamed.sha256,
            size=streamed.size,
            is_binary=streamed.is_binary,
            content=streamed.content,
            mode=entry.mode,
            gitlink_oid=None,
        )
    return _regular_blob_row(path, "  ", streamed, entry)


def _read_final_head_file(
    root: Path,
    head: str,
    path: str,
    max_size: int,
    deadline: float,
    tick: Callable[[], float],
    budget: HashBudget,
) -> BaselineFileRow | None:
    # Retained for compatibility; new code prefers _resolve_final which
    # distinguishes structural modes honestly instead of failing.
    entry = _ls_tree_entry(root, head, path, deadline, tick)
    if entry is None or entry.kind == "tree":
        return None
    if _is_gitlink_entry(entry):
        return _gitlink_row(path, _CLEAN_FINAL_STATUS, entry)
    if _is_symlink_entry(entry):
        streamed = _stream_git_blob_to_row(
            root,
            head,
            path,
            _CLEAN_FINAL_STATUS,
            max_size,
            deadline,
            tick,
            budget,
        )
        link_bytes: bytes | None = None
        if streamed.content is not None:
            try:
                link_bytes = gzip.decompress(bytes(streamed.content))
            except OSError:
                link_bytes = None
        if link_bytes is not None:
            return _symlink_row(
                path, _CLEAN_FINAL_STATUS, link_bytes, max_size
            )
        return BaselineFileRow(
            path=streamed.path,
            status=_CLEAN_FINAL_STATUS,
            sha256=streamed.sha256,
            size=streamed.size,
            is_binary=streamed.is_binary,
            content=streamed.content,
            mode=entry.mode,
            gitlink_oid=None,
        )
    if entry.kind != "blob" or entry.mode not in _REGULAR_BLOB_MODES:
        return BaselineFileRow(
            path=path,
            status=_CLEAN_FINAL_STATUS,
            sha256=None,
            size=None,
            is_binary=None,
            content=None,
            mode=entry.mode,
            gitlink_oid=None,
        )
    streamed = _stream_git_blob_to_row(
        root,
        head,
        path,
        _CLEAN_FINAL_STATUS,
        max_size,
        deadline,
        tick,
        budget,
    )
    return _regular_blob_row(path, _CLEAN_FINAL_STATUS, streamed, entry)


def _match_tree_entry(
    tree_out: bytes,
    path: str,
) -> _TreeEntry | None:
    _reject_lossy_path(path)
    try:
        target = path.encode("utf-8")
    except UnicodeEncodeError:
        raise FinalizationError("UNSUPPORTED_FINAL_PATH") from None
    for record in tree_out.split(b"\0"):
        if not record:
            continue
        meta, separator, name = record.partition(b"\t")
        if not separator:
            raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE")
        if name != target:
            continue
        parts = meta.split(b" ")
        if len(parts) != 3:
            raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE")
        try:
            mode = parts[0].decode("ascii")
            kind = parts[1].decode("ascii")
            oid = parts[2].decode("ascii")
        except UnicodeDecodeError:
            raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from None
        if not oid or any(
            character not in "0123456789abcdef" for character in oid
        ):
            raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from None
        return _TreeEntry(mode=mode, kind=kind, oid=oid)
    return None


def _parse_ls_files(raw: bytes) -> dict[str, _IndexEntry]:
    # Live index metadata source (portable, includes staged chmod).
    # Per-path entries; stage != "0" (unmerged) fails closed. Non-UTF8
    # paths fail explicitly without lossy decoding. This is live
    # `git ls-files -s -z` output, never the stored canonical manifest.
    entries: dict[str, _IndexEntry] = {}
    if not raw:
        return entries
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        if not separator or not raw_path:
            raise FinalizationError("FINAL_CAPTURE_FAILED") from None
        try:
            text = header.decode("ascii")
        except UnicodeDecodeError:
            raise FinalizationError("FINAL_CAPTURE_FAILED") from None
        parts = text.split(" ")
        if len(parts) != 3:
            raise FinalizationError("FINAL_CAPTURE_FAILED") from None
        mode, oid, stage = parts
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            raise FinalizationError("UNSUPPORTED_FINAL_PATH") from None
        _reject_lossy_path(path)
        if stage != "0":
            raise FinalizationError("UNSUPPORTED_FINAL_PATH") from None
        entries[path] = _IndexEntry(mode=mode, oid=oid, stage=stage)
    return entries


def _ls_files_map(
    root: Path, deadline: float, tick: Callable[[], float]
) -> dict[str, _IndexEntry]:
    try:
        raw = _git(root, ["ls-files", "-s", "-z"], deadline, tick)
    except FinalizationError as error:
        if error.code in ("FINAL_SNAPSHOT_TIMEOUT", "FINAL_HASH_TIMEOUT"):
            raise
        raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from error
    try:
        return _parse_ls_files(raw)
    except FinalizationError as error:
        if error.code == "UNSUPPORTED_FINAL_PATH":
            raise
        raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from error


def _worktree_diff_new_mode(
    root: Path, path: str, deadline: float, tick: Callable[[], float]
) -> str | None:
    # Portable worktree mode source for unstaged-only alterations:
    # `git diff --raw -z -- <path>` is computed by Git via lstat
    # respecting core.filemode, so Windows (filemode=false) never
    # reports a pure worktree mode change while Unix does. Returns the
    # worktree (new) mode or None when there is no worktree-vs-index
    # diff for the path (caller treats inconsistency as unstable).
    try:
        raw = _git(root, ["diff", "--raw", "-z", "--", path], deadline, tick)
    except FinalizationError as error:
        if error.code in ("FINAL_SNAPSHOT_TIMEOUT", "FINAL_HASH_TIMEOUT"):
            raise
        raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from error
    records = [item for item in raw.split(b"\0") if item]
    if not records:
        return None
    header = records[0]
    if not header.startswith(b":"):
        raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from None
    parts = header[1:].split(b" ")
    if len(parts) < 2:
        raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from None
    try:
        return parts[1].decode("ascii")
    except UnicodeDecodeError:
        raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from None


def _read_final_file(
    root: Path,
    path: str,
    status: str | None,
    max_size: int,
    deadline: float,
    tick: Callable[[], float],
    open_fn: Callable[..., Any],
    budget: HashBudget,
    file_started_at: float,
) -> BaselineFileRow | None:
    # Deleted worktree entries stay absent; clean-absent paths stay absent.
    # Symlink/special worktree kinds are resolved by _resolve_final without
    # following the link; this helper only hashes regular worktree files.
    if status is not None and "D" in status:
        return None
    target = root / path
    try:
        if status is None and not target.exists():
            return None
        if target.is_symlink():
            raise OSError
        if not target.is_file():
            raise OSError
        hashed = _hash_worktree_file(
            target,
            max_size,
            deadline,
            tick,
            open_fn,
            budget,
            file_started_at,
        )
    except OSError as error:
        elapsed = tick() - file_started_at
        budget.commit(elapsed)
        if (
            elapsed > budget.per_file_budget
            or budget.spent > budget.aggregate_budget
        ):
            raise FinalizationError("FINAL_HASH_TIMEOUT") from error
        raise FinalizationError("FINAL_SNAPSHOT_UNSTABLE") from error
    if tick() >= deadline:
        raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
    return BaselineFileRow(
        path=path,
        status=status or "  ",
        sha256=hashed.sha256,
        size=hashed.size,
        is_binary=hashed.is_binary,
        content=(bytes(hashed.data) if hashed.data is not None else None),
    )


def _ls_tree_entry(
    root: Path,
    head: str,
    path: str,
    deadline: float,
    tick: Callable[[], float],
) -> _TreeEntry | None:
    # Genuinely missing objects fail closed as unavailable; absent paths
    # return None. Used for both baseline freeze and final resolution so
    # structural modes never claim unavailable content.
    try:
        tree_out = _git(
            root, ["ls-tree", "-z", head, "--", path], deadline, tick
        )
    except FinalizationError as error:
        if error.code in ("FINAL_SNAPSHOT_TIMEOUT", "FINAL_HASH_TIMEOUT"):
            raise
        raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from error
    return _match_tree_entry(tree_out, path)


def _is_symlink_entry(entry: _TreeEntry | None) -> bool:
    return (
        entry is not None and entry.kind == "blob" and entry.mode == "120000"
    )


def _is_gitlink_entry(entry: _TreeEntry | None) -> bool:
    return (
        entry is not None and entry.mode == "160000" and entry.kind == "commit"
    )


def _is_regular_entry(entry: _TreeEntry | None) -> bool:
    return (
        entry is not None
        and entry.kind == "blob"
        and entry.mode in _REGULAR_BLOB_MODES
    )


def _reason_for_entry(entry: _TreeEntry | None) -> str | None:
    if entry is None:
        return None
    if _is_symlink_entry(entry):
        return REASON_SYMLINK
    if _is_gitlink_entry(entry):
        return REASON_GITLINK
    if entry.kind == "tree":
        return None
    if not _is_regular_entry(entry):
        return REASON_SPECIAL_FILE
    return None


def _entry_kind(entry: _TreeEntry | None) -> str:
    if entry is None:
        return "absent"
    if entry.kind == "tree":
        return "tree"
    if _is_gitlink_entry(entry):
        return "gitlink"
    if _is_symlink_entry(entry):
        return "symlink"
    if _is_regular_entry(entry):
        return "regular"
    return "special"


def _symlink_row(
    path: str, status: str, link_bytes: bytes, max_size: int
) -> BaselineFileRow:
    # Preserve link target bytes without following the destination.
    # Structural mode 120000 is persisted; target hash/bytes prove
    # no-follow (destination content never hashed).
    digest = hashlib.sha256(link_bytes).hexdigest()
    saved = None
    if len(link_bytes) <= max_size:
        saved = gzip.compress(bytes(link_bytes))
    return BaselineFileRow(
        path=path,
        status=status,
        sha256=digest,
        size=len(link_bytes),
        is_binary=0,
        content=saved,
        mode="120000",
        gitlink_oid=None,
    )


def _gitlink_row(path: str, status: str, entry: _TreeEntry) -> BaselineFileRow:
    # Structural gitlink identity: mode + commit OID, never internal
    # content. OID/mode advances always surface as changes.
    return BaselineFileRow(
        path=path,
        status=status,
        sha256=None,
        size=None,
        is_binary=None,
        content=None,
        mode=entry.mode,
        gitlink_oid=entry.oid,
    )


def _regular_blob_row(
    path: str,
    status: str,
    row: BaselineFileRow,
    entry: _TreeEntry | None,
) -> BaselineFileRow:
    return BaselineFileRow(
        path=row.path,
        status=status,
        sha256=row.sha256,
        size=row.size,
        is_binary=row.is_binary,
        content=row.content,
        mode=entry.mode if entry is not None else None,
        gitlink_oid=None,
    )


def _readlink_bytes(target: Path) -> bytes:
    # No-follow link read preserving raw bytes; explicit failure without
    # lossy decoding so non-UTF8 targets never become surrogate strings.
    try:
        raw = os.readlink(os.fsencode(target))
    except OSError as error:
        raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from error
    if isinstance(raw, str):
        raw_bytes = os.fsencode(raw)
    else:
        raw_bytes = bytes(raw)
    return raw_bytes


def _worktree_kind(
    root: Path,
    path: str,
    xy: str | None,
    final_entry: _TreeEntry | None,
) -> str:
    if xy is not None and xy in ("D ", " D"):
        return "deleted"
    if xy is not None and xy in ("DD", "AA"):
        raise FinalizationError("UNSUPPORTED_FINAL_PATH") from None
    target = root / path
    try:
        if target.is_symlink():
            return "symlink"
    except OSError:
        raise FinalizationError("FINAL_SNAPSHOT_UNSTABLE") from None
    try:
        if target.is_file():
            return "regular"
    except OSError:
        raise FinalizationError("FINAL_SNAPSHOT_UNSTABLE") from None
    try:
        exists = target.exists() or target.is_dir()
    except OSError:
        raise FinalizationError("FINAL_SNAPSHOT_UNSTABLE") from None
    if not exists:
        if xy is None:
            return "absent"
        # Expected present but missing: race with the stability reads.
        raise FinalizationError("FINAL_SNAPSHOT_UNSTABLE") from None
    if _is_gitlink_entry(final_entry):
        return "gitlink"
    return "special"


def _freeze_baseline_entry(
    root: Path,
    head: str,
    path: str,
    entry: _TreeEntry | None,
    max_size: int,
    deadline: float,
    tick: Callable[[], float],
    budget: HashBudget,
) -> BaselineFileRow | None:
    if entry is None or entry.kind == "tree":
        return None
    if _is_gitlink_entry(entry):
        return _gitlink_row(path, "  ", entry)
    if not _is_symlink_entry(entry) and not _is_regular_entry(entry):
        return BaselineFileRow(
            path=path,
            status="  ",
            sha256=None,
            size=None,
            is_binary=None,
            content=None,
            mode=entry.mode,
            gitlink_oid=None,
        )
    streamed = _stream_git_blob_to_row(
        root, head, path, "  ", max_size, deadline, tick, budget
    )
    if _is_symlink_entry(entry):
        link_bytes: bytes | None = None
        if streamed.content is not None:
            try:
                link_bytes = gzip.decompress(bytes(streamed.content))
            except OSError:
                link_bytes = None
        if link_bytes is not None:
            return _symlink_row(path, "  ", link_bytes, max_size)
    return _regular_blob_row(path, "  ", streamed, entry)
    if not _is_symlink_entry(entry) and not _is_regular_entry(entry):
        return BaselineFileRow(
            path=path,
            status="  ",
            sha256=None,
            size=None,
            is_binary=None,
            content=None,
        )
    return _stream_git_blob_to_row(
        root, head, path, "  ", max_size, deadline, tick, budget
    )


def _cat_index_blob(
    root: Path, oid: str, deadline: float, tick: Callable[[], float]
) -> bytes:
    # Live object read for staged blobs (e.g., staged symlink target
    # without a worktree link). Small structural payloads only; no hash
    # budget charged (caller hashes bounded link bytes, not worktree).
    try:
        return _git(root, ["cat-file", "-p", oid], deadline, tick)
    except FinalizationError as error:
        if error.code in ("FINAL_SNAPSHOT_TIMEOUT", "FINAL_HASH_TIMEOUT"):
            raise
        raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from error


def _worktree_gitlink_oid(
    root: Path, path: str, deadline: float, tick: Callable[[], float]
) -> str | None:
    # Structural submodule pointer only (commit OID), never file content.
    # Reads the linked worktree HEAD without entering its history.
    timeout = min(SUBPROCESS_TIMEOUT_SECONDS, deadline - tick())
    if timeout <= 0:
        raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
    try:
        result = subprocess.run(
            ["git", "-C", str(root / path), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT") from error
    except OSError as error:
        if tick() >= deadline:
            raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT") from error
        return None
    if tick() >= deadline:
        raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
    if result.returncode != 0:
        return None
    try:
        oid = result.stdout.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if len(oid) not in (40, 64) or any(
        character not in "0123456789abcdef" for character in oid
    ):
        return None
    return oid


def _gitlink_row_from_index(
    path: str, status: str, entry: _IndexEntry
) -> BaselineFileRow:
    return BaselineFileRow(
        path=path,
        status=status,
        sha256=None,
        size=None,
        is_binary=None,
        content=None,
        mode=entry.mode,
        gitlink_oid=entry.oid,
    )


def _resolve_final(
    root: Path,
    final_head: str,
    path: str,
    final_entry: _TreeEntry | None,
    index_entry: _IndexEntry | None,
    xy: str | None,
    max_size: int,
    deadline: float,
    tick: Callable[[], float],
    open_fn: Callable[..., Any],
    budget: HashBudget,
) -> tuple[BaselineFileRow | None, str | None]:
    # Effective-mode contract (Windows-portable, staged+unstaged).
    # Invariant: final_* rows represent the effective final WORKTREE;
    # the staged index is separate evidence (final_index_manifest).
    # - clean (xy None): HEAD ls-tree is authoritative (mode+oid).
    # - staged-only (Y == ' '): worktree matches the index (Y clean),
    #   so live index (`git ls-files -s -z`, bulk per capture, never
    #   the stored manifest) equals the worktree mode/OID.
    # - untracked (`??`): worktree lstat/hash is authoritative; regular
    #   files have no Git mode yet (None, honest), symlinks persist
    #   120000 + target bytes no-follow.
    # - worktree dirt (Y in M/T): worktree content is authoritative and
    #   so is the worktree MODE, read from `git diff --raw -z -- <path>`
    #   (Git-computed via lstat, respects core.filemode). The index is
    #   NEVER used as final mode when Y diverges -- e.g. staged chmod
    #   (index 100755) plus unstaged reversal (worktree 100644) reports
    #   `MM` and must resolve to the worktree 100644, not the index.
    #   A missing worktree (Y == 'D') is always absence (None), exactly
    #   like regular files; staged structural identity lives on only in
    #   the index manifest, never as a final_* row.
    # Pure deletions (`D `/` D`/any Y == 'D') resolve to None.
    # Unmerged (`DD`/`AA`) fails.
    if xy is not None and xy in ("DD", "AA"):
        raise FinalizationError("UNSUPPORTED_FINAL_PATH") from None
    if xy is not None and (xy in ("D ", " D") or xy[1] == "D"):
        return None, None
    if xy is None:
        if final_entry is None or final_entry.kind == "tree":
            return None, None
        if _is_gitlink_entry(final_entry):
            return _gitlink_row(path, _CLEAN_FINAL_STATUS, final_entry), (
                REASON_GITLINK
            )
        if _is_symlink_entry(final_entry):
            row = _stream_git_blob_to_row(
                root,
                final_head,
                path,
                _CLEAN_FINAL_STATUS,
                max_size,
                deadline,
                tick,
                budget,
            )
            link_bytes: bytes | None = None
            if row.content is not None:
                try:
                    link_bytes = gzip.decompress(bytes(row.content))
                except OSError:
                    link_bytes = None
            if link_bytes is not None:
                row = _symlink_row(
                    path, _CLEAN_FINAL_STATUS, link_bytes, max_size
                )
            else:
                row = BaselineFileRow(
                    path=row.path,
                    status=_CLEAN_FINAL_STATUS,
                    sha256=row.sha256,
                    size=row.size,
                    is_binary=row.is_binary,
                    content=row.content,
                    mode=final_entry.mode,
                    gitlink_oid=None,
                )
            return row, REASON_SYMLINK
        if not _is_regular_entry(final_entry):
            return (
                BaselineFileRow(
                    path=path,
                    status=_CLEAN_FINAL_STATUS,
                    sha256=None,
                    size=None,
                    is_binary=None,
                    content=None,
                    mode=final_entry.mode,
                    gitlink_oid=None,
                ),
                REASON_SPECIAL_FILE,
            )
        row = _stream_git_blob_to_row(
            root,
            final_head,
            path,
            _CLEAN_FINAL_STATUS,
            max_size,
            deadline,
            tick,
            budget,
        )
        return _regular_blob_row(
            path, _CLEAN_FINAL_STATUS, row, final_entry
        ), (None)
    x, y = xy[0], xy[1]
    if xy == "??" or x == "?":
        kind = _worktree_kind(root, path, xy, final_entry)
        if kind == "symlink":
            link_bytes = _readlink_bytes(root / path)
            return (
                _symlink_row(path, xy, link_bytes, max_size),
                REASON_SYMLINK,
            )
        if kind in ("gitlink", "special"):
            if kind == "gitlink" and index_entry is not None:
                oid = _worktree_gitlink_oid(root, path, deadline, tick)
                entry = _IndexEntry(
                    mode=index_entry.mode,
                    oid=oid or index_entry.oid,
                    stage="0",
                )
                return (
                    _gitlink_row_from_index(path, xy, entry),
                    REASON_GITLINK,
                )
            return (
                BaselineFileRow(
                    path=path,
                    status=xy,
                    sha256=None,
                    size=None,
                    is_binary=None,
                    content=None,
                    mode=None,
                    gitlink_oid=None,
                ),
                REASON_SPECIAL_FILE,
            )
        file_started_at = tick()
        row = _read_final_file(
            root,
            path,
            xy,
            max_size,
            deadline,
            tick,
            open_fn,
            budget,
            file_started_at,
        )
        if row is not None:
            row = BaselineFileRow(
                path=row.path,
                status=row.status,
                sha256=row.sha256,
                size=row.size,
                is_binary=row.is_binary,
                content=row.content,
                mode=None,
                gitlink_oid=None,
            )
        return row, None
    worktree_dirt = y != " "
    if not worktree_dirt:
        # Staged-only: index authoritative for mode/OID.
        if index_entry is None:
            if x == "D":
                return None, None
            raise FinalizationError("FINAL_SNAPSHOT_UNSTABLE") from None
        if index_entry.mode == "160000":
            return (
                _gitlink_row_from_index(path, xy, index_entry),
                REASON_GITLINK,
            )
        if index_entry.mode == "120000":
            target = root / path
            try:
                is_link = target.is_symlink()
            except OSError:
                raise FinalizationError("FINAL_SNAPSHOT_UNSTABLE") from None
            if is_link:
                link_bytes = _readlink_bytes(target)
            else:
                # Staged symlink without a worktree link (e.g., plumbing
                # on Windows): link bytes come from the staged blob.
                link_bytes = _cat_index_blob(
                    root, index_entry.oid, deadline, tick
                )
            return (
                _symlink_row(path, xy, link_bytes, max_size),
                REASON_SYMLINK,
            )
        if index_entry.mode in _REGULAR_BLOB_MODES:
            file_started_at = tick()
            row = _read_final_file(
                root,
                path,
                xy,
                max_size,
                deadline,
                tick,
                open_fn,
                budget,
                file_started_at,
            )
            if row is None:
                raise FinalizationError("FINAL_SNAPSHOT_UNSTABLE") from None
            row = BaselineFileRow(
                path=row.path,
                status=row.status,
                sha256=row.sha256,
                size=row.size,
                is_binary=row.is_binary,
                content=row.content,
                mode=index_entry.mode,
                gitlink_oid=None,
            )
            return row, None
        return (
            BaselineFileRow(
                path=path,
                status=xy,
                sha256=None,
                size=None,
                is_binary=None,
                content=None,
                mode=index_entry.mode,
                gitlink_oid=None,
            ),
            REASON_SPECIAL_FILE,
        )
    # Any Y == 'D' (including `AD`/`MD` staged structural): worktree
    # truth wins (None), exactly like regular files -- the staged
    # identity lives on only in the index manifest, never as final_*.
    kind = _worktree_kind(root, path, xy, final_entry)
    if kind == "deleted":
        return None, None
    if kind == "symlink":
        link_bytes = _readlink_bytes(root / path)
        return (
            _symlink_row(path, xy, link_bytes, max_size),
            REASON_SYMLINK,
        )
    if kind in ("gitlink", "special"):
        if index_entry is not None and index_entry.mode == "160000":
            oid = _worktree_gitlink_oid(root, path, deadline, tick)
            entry = _IndexEntry(
                mode=index_entry.mode,
                oid=oid or index_entry.oid,
                stage="0",
            )
            return _gitlink_row_from_index(path, xy, entry), REASON_GITLINK
        if _is_gitlink_entry(final_entry) and final_entry.oid is not None:
            oid = _worktree_gitlink_oid(root, path, deadline, tick)
            row_entry = _TreeEntry(
                mode="160000", kind="commit", oid=oid or final_entry.oid
            )
            return _gitlink_row(path, xy, row_entry), REASON_GITLINK
        reason = REASON_GITLINK if kind == "gitlink" else REASON_SPECIAL_FILE
        return (
            BaselineFileRow(
                path=path,
                status=xy,
                sha256=None,
                size=None,
                is_binary=None,
                content=None,
                mode=(
                    index_entry.mode
                    if index_entry is not None
                    else (
                        final_entry.mode if final_entry is not None else None
                    )
                ),
                gitlink_oid=(
                    index_entry.oid
                    if index_entry is not None and index_entry.mode == "160000"
                    else None
                ),
            ),
            reason,
        )
    file_started_at = tick()
    row = _read_final_file(
        root,
        path,
        xy,
        max_size,
        deadline,
        tick,
        open_fn,
        budget,
        file_started_at,
    )
    if row is not None:
        # Worktree content AND worktree mode are authoritative whenever
        # Y diverges (covers X/Y combined: ` M`, `MM`, `AM`, `TM`...).
        # The worktree mode comes from `git diff --raw -z -- <path>`
        # (Git-computed via lstat, respects core.filemode); the index
        # is never used as final mode here -- e.g. staged 100755 plus
        # unstaged reversal reports `MM` and must resolve to worktree
        # 100644. An empty diff against a Y-diverged status is a race:
        # fail closed as unstable for the stability retry. A diff mode
        # outside the regular set against a regular lstat is the same
        # race (type changed under us).
        worktree_mode = _worktree_diff_new_mode(root, path, deadline, tick)
        if worktree_mode is None:
            raise FinalizationError("FINAL_SNAPSHOT_UNSTABLE") from None
        if worktree_mode not in _REGULAR_BLOB_MODES:
            raise FinalizationError("FINAL_SNAPSHOT_UNSTABLE") from None
        row = BaselineFileRow(
            path=row.path,
            status=row.status,
            sha256=row.sha256,
            size=row.size,
            is_binary=row.is_binary,
            content=row.content,
            mode=worktree_mode,
            gitlink_oid=None,
        )
    return row, None


def _is_mode_only_change(
    initial: BaselineFileRow | None,
    final: BaselineFileRow | None,
    initial_entry: _TreeEntry | None,
    final_entry: _TreeEntry | None,
    xy: str | None,
) -> bool:
    # Mode-only is decided on persisted structural modes (not content):
    # identical bytes with different regular modes are partial honest.
    # Callers supplement worktree/index effective modes before invoking.
    if initial is None or final is None:
        return False
    if not _same_content(initial, final):
        return False
    initial_mode = initial.mode or (
        initial_entry.mode if initial_entry is not None else None
    )
    final_mode = final.mode or (
        final_entry.mode if final_entry is not None else None
    )
    if initial_mode is None or final_mode is None:
        return False
    if initial_mode not in _REGULAR_BLOB_MODES:
        return False
    if final_mode not in _REGULAR_BLOB_MODES:
        return False
    return initial_mode != final_mode


def _same_content(
    initial: BaselineFileRow | None, final: BaselineFileRow | None
) -> bool:
    if initial is None or final is None:
        return initial is final
    return initial.sha256 == final.sha256 and initial.size == final.size


def _hash_worktree_file(
    target: Path,
    max_size: int,
    deadline: float,
    tick: Callable[[], float],
    open_fn: Callable[..., Any],
    budget: HashBudget,
    file_started_at: float,
) -> HashedContent:
    return hash_worktree_file(
        target,
        max_size,
        tick=tick,
        open_fn=open_fn,
        budget=budget,
        file_started_at=file_started_at,
        deadline=deadline,
        deadline_error=lambda: FinalizationError("FINAL_SNAPSHOT_TIMEOUT"),
        budget_error=lambda: FinalizationError("FINAL_HASH_TIMEOUT"),
    )


def _stream_git_blob_to_row(
    root: Path,
    head: str,
    path: str,
    status: str,
    max_size: int,
    deadline: float,
    tick: Callable[[], float],
    budget: HashBudget,
) -> BaselineFileRow:
    """Stream one ``git show`` blob without buffering it fully.

    Spawns ``git show`` with piped stdout and hashes chunks
    incrementally under the absolute deadline and aggregate hash
    budget. Each ``stdout.read`` runs with a wall-clock timeout
    bounded by the remaining deadline/subprocess budget on a daemon
    thread, so a blocked producer cannot hang the caller; on timeout
    the process is killed/reaped and ``FINAL_SNAPSHOT_TIMEOUT`` is
    raised without leaving the thread/process blocking the return.
    Retention is capped at ``max_size + 1`` bytes. The subprocess is
    always reaped fail-safe; ``OSError`` mid-stream charges elapsed
    once into ``budget`` and promotes deadline/hash exhaustion before
    mapping the remainder to ``BASELINE_OBJECT_UNAVAILABLE``. The
    ``proc.wait`` for ``git show`` exit is bounded by the remaining
    deadline, subprocess, per-file and aggregate hash budgets, and
    its wait time is charged into ``budget`` before commit, so
    budget exhaustion during the wait raises ``FINAL_HASH_TIMEOUT``.
    Non-zero exit prioritizes an expired deadline while deadline and
    hash-budget errors propagate with final-capture codes.
    """
    file_started_at = tick()
    timeout = min(SUBPROCESS_TIMEOUT_SECONDS, deadline - file_started_at)
    if timeout <= 0:
        raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
    try:
        proc = subprocess.Popen(
            ["git", "-C", str(root), "show", f"{head}:{path}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        if tick() >= deadline:
            raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT") from error
        raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from error
    try:
        if proc.stdout is None:
            raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE")
        try:
            streamed = hash_stream(
                proc.stdout,
                max_size,
                tick=tick,
                budget=budget,
                file_started_at=file_started_at,
                deadline=deadline,
                deadline_error=lambda: FinalizationError(
                    "FINAL_SNAPSHOT_TIMEOUT"
                ),
                budget_error=lambda: FinalizationError("FINAL_HASH_TIMEOUT"),
                subprocess_timeout=SUBPROCESS_TIMEOUT_SECONDS,
            )
        except OSError as error:
            elapsed = tick() - file_started_at
            budget.commit(elapsed)
            if tick() >= deadline:
                raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT") from error
            if (
                elapsed > budget.per_file_budget
                or budget.spent > budget.aggregate_budget
            ):
                raise FinalizationError("FINAL_HASH_TIMEOUT") from error
            raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from error
        hashed = streamed.content
        stream_now = streamed.finished_at
        elapsed_at_stream = stream_now - file_started_at
        deadline_remaining = deadline - stream_now
        subprocess_remaining = SUBPROCESS_TIMEOUT_SECONDS - elapsed_at_stream
        file_remaining = budget.per_file_budget - elapsed_at_stream
        aggregate_remaining = budget.aggregate_budget - (
            budget.spent + elapsed_at_stream
        )
        deadline_like = min(deadline_remaining, subprocess_remaining)
        budget_like = min(file_remaining, aggregate_remaining)
        remaining = min(deadline_like, budget_like)
        if remaining <= 0:
            if deadline_like <= budget_like:
                raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
            raise FinalizationError("FINAL_HASH_TIMEOUT")
        try:
            returncode = proc.wait(timeout=max(remaining, 0.001))
        except subprocess.TimeoutExpired as error:
            if deadline_like <= budget_like:
                raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT") from error
            raise FinalizationError("FINAL_HASH_TIMEOUT") from error
        wait_now = tick()
        if wait_now - file_started_at > SUBPROCESS_TIMEOUT_SECONDS:
            raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
        budget.check(
            now=wait_now,
            file_started_at=file_started_at,
            deadline=deadline,
            deadline_error=lambda: FinalizationError("FINAL_SNAPSHOT_TIMEOUT"),
            budget_error=lambda: FinalizationError("FINAL_HASH_TIMEOUT"),
        )
        if returncode != 0:
            raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE")
        budget.commit(wait_now - file_started_at)
        saved = None
        if not hashed.is_binary and hashed.size <= max_size:
            saved = gzip.compress(bytes(hashed.data))
        if tick() >= deadline:
            raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
        return BaselineFileRow(
            path=path,
            status=status,
            sha256=hashed.sha256,
            size=hashed.size,
            is_binary=hashed.is_binary,
            content=saved,
        )
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            close = getattr(proc.stdout, "close", None)
            if callable(close):
                close()
        except OSError:
            pass


def _same_file(
    initial: BaselineFileRow | None, final: BaselineFileRow | None
) -> bool:
    # Structural identity: content (sha+size) plus mode/OID when known.
    # Gitlink OID advances must never disappear: two gitlinks with equal
    # (None) content but different OIDs are different. Legacy rows with
    # unknown (None) mode/OID are treated as wildcards for that axis so
    # old data does not spuriously become mode-only.
    if initial is None or final is None:
        return initial is final
    if initial.sha256 != final.sha256 or initial.size != final.size:
        return False
    if (
        initial.mode is not None
        and final.mode is not None
        and initial.mode != final.mode
    ):
        return False
    if (initial.gitlink_oid is None) != (final.gitlink_oid is None):
        return False
    if (
        initial.gitlink_oid is not None
        and final.gitlink_oid is not None
        and initial.gitlink_oid != final.gitlink_oid
    ):
        return False
    return True


def _evidence_for_change(
    initial: BaselineFileRow | None,
    final: BaselineFileRow | None,
    structural_reason: str | None,
) -> tuple[str, str | None]:
    if structural_reason is not None:
        return EVIDENCE_UNSUPPORTED, structural_reason
    rows = [row for row in (initial, final) if row is not None]
    if not rows:
        return EVIDENCE_COMPLETE, None
    if all(row.content is not None for row in rows):
        return EVIDENCE_COMPLETE, None
    if any(row.is_binary for row in rows):
        return EVIDENCE_HASH_ONLY, REASON_BINARY_CONTENT
    return EVIDENCE_HASH_ONLY, REASON_SIZE_LIMIT


def _evidence_status(
    initial: BaselineFileRow | None, final: BaselineFileRow | None
) -> str:
    status, _ = _evidence_for_change(initial, final, None)
    return status


def _evidence_reason(
    initial: BaselineFileRow | None, final: BaselineFileRow | None
) -> str | None:
    _, reason = _evidence_for_change(initial, final, None)
    return reason


def _git(
    root: Path,
    arguments: list[str],
    deadline: float,
    tick: Callable[[], float],
) -> bytes:
    timeout = min(SUBPROCESS_TIMEOUT_SECONDS, deadline - tick())
    if timeout <= 0:
        raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
    try:
        stdout = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            timeout=timeout,
        ).stdout
    except subprocess.TimeoutExpired as error:
        raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT") from error
    except (subprocess.CalledProcessError, OSError) as error:
        if tick() >= deadline:
            raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT") from error
        raise FinalizationError("FINAL_CAPTURE_FAILED") from error
    if tick() >= deadline:
        raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
    return stdout
