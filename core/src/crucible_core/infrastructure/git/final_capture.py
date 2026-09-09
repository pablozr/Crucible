from __future__ import annotations

import gzip
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


@dataclass
class _PathEvidence:
    baseline_files: list[BaselineFileRow] = field(default_factory=list)
    changes: list[TaskFileChangeRow] = field(default_factory=list)


@dataclass(frozen=True)
class _TreeEntry:
    mode: str
    kind: str


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
    paths = sorted(set(initial_rows) | set(final_states) | committed)
    frozen_baselines: list[BaselineFileRow] = []
    changes: list[TaskFileChangeRow] = []

    for path in paths:
        stored_initial = initial_rows.get(path)
        initial = stored_initial
        if initial is not None and "D" in initial.status:
            initial = None
        if stored_initial is None:
            initial = _read_head_file(
                root,
                baseline_head,
                path,
                max_size,
                deadline,
                tick,
                budget,
            )
            if initial is not None:
                frozen_baselines.append(initial)
        final = _read_final_head_file(
            root, final_head, path, max_size, deadline, tick, budget
        )
        if final_states.get(path) is not None:
            file_started_at = tick()
            final = _read_final_file(
                root,
                path,
                final_states.get(path),
                max_size,
                deadline,
                tick,
                open_fn,
                budget,
                file_started_at,
            )
        if _same_file(initial, final):
            continue

        operation = "modified"
        if initial is None:
            operation = "added"
        elif final is None:
            operation = "deleted"
        changes.append(
            TaskFileChangeRow(
                path=path,
                operation=operation,
                final_status=final_states.get(path, "  "),
                final_sha256=final.sha256 if final else None,
                final_size=final.size if final else None,
                final_is_binary=final.is_binary if final else None,
                final_content=final.content if final else None,
                evidence_status=_evidence_status(initial, final),
                evidence_reason=_evidence_reason(initial, final),
            )
        )
    return _PathEvidence(baseline_files=frozen_baselines, changes=changes)


def _parse_status(status: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in status.split(b"\0"):
        if not entry:
            continue
        xy = entry[:2].decode("ascii")
        if "R" in xy or "C" in xy:
            raise FinalizationError("UNSUPPORTED_FINAL_PATH")
        path = entry[3:].decode("utf-8", "surrogateescape")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in path):
            raise FinalizationError("UNSUPPORTED_FINAL_PATH")
        result[path] = xy
    return result


def _evidence_identity(
    evidence: _PathEvidence,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    baselines = [
        (row.path, row.status, row.sha256, row.size)
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
    try:
        tree_out = _git(
            root, ["ls-tree", "-z", head, "--", path], deadline, tick
        )
    except FinalizationError as error:
        if error.code in ("FINAL_SNAPSHOT_TIMEOUT", "FINAL_HASH_TIMEOUT"):
            raise
        raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from error
    matched = _match_tree_entry(tree_out, path)
    if matched is None:
        return None
    if matched.kind == "tree":
        return None
    return _stream_git_blob_to_row(
        root, head, path, "  ", max_size, deadline, tick, budget
    )


def _read_final_head_file(
    root: Path,
    head: str,
    path: str,
    max_size: int,
    deadline: float,
    tick: Callable[[], float],
    budget: HashBudget,
) -> BaselineFileRow | None:
    try:
        tree_out = _git(
            root, ["ls-tree", "-z", head, "--", path], deadline, tick
        )
    except FinalizationError as error:
        if error.code in ("FINAL_SNAPSHOT_TIMEOUT", "FINAL_HASH_TIMEOUT"):
            raise
        raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from error
    matched = _match_tree_entry(tree_out, path)
    if matched is None:
        return None
    if matched.kind == "tree":
        return None
    if matched.kind != "blob" or matched.mode not in _REGULAR_BLOB_MODES:
        raise FinalizationError("FINAL_SNAPSHOT_UNSTABLE")
    return _stream_git_blob_to_row(
        root,
        head,
        path,
        _CLEAN_FINAL_STATUS,
        max_size,
        deadline,
        tick,
        budget,
    )


def _match_tree_entry(
    tree_out: bytes,
    path: str,
) -> _TreeEntry | None:
    target = path.encode("utf-8")
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
        except UnicodeDecodeError:
            raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from None
        return _TreeEntry(mode=mode, kind=kind)
    return None


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
    if status is not None and "D" in status:
        return None
    target = root / path
    try:
        if status is None and not target.exists():
            return None
        if not target.is_file() or target.is_symlink():
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
    if initial is None or final is None:
        return initial is final
    return initial.sha256 == final.sha256 and initial.size == final.size


def _evidence_status(
    initial: BaselineFileRow | None, final: BaselineFileRow | None
) -> str:
    rows = [row for row in (initial, final) if row is not None]
    if all(row.content is not None for row in rows):
        return "complete"
    return "hash_only"


def _evidence_reason(
    initial: BaselineFileRow | None, final: BaselineFileRow | None
) -> str | None:
    rows = [row for row in (initial, final) if row is not None]
    if all(row.content is not None for row in rows):
        return None
    if any(row.is_binary for row in rows):
        return "BINARY_CONTENT"
    return "SNAPSHOT_SIZE_LIMIT"


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
