from __future__ import annotations

import gzip
import hashlib
import subprocess
import time
from pathlib import Path
from typing import Any

from crucible_core.core.errors import FinalizationError
from crucible_core.infrastructure.git.index_manifest import (
    build_canonical_manifest,
)
from crucible_core.schemas.persistence import (
    BaselineFileRow,
    TaskFileChangeRow,
)

SUBPROCESS_TIMEOUT_SECONDS = 2


def capture_final(
    root: Path,
    baseline_head: str,
    baseline_branch: str,
    baseline_index: bytes,
    baseline_files: list[BaselineFileRow],
    max_size: int,
    deadline: float,
) -> dict[str, Any]:
    # baseline_index is retained for signature compatibility; index
    # changes are allowed in slice 7.2 and final index is evidence only.
    _ = baseline_index
    first = _git_state(root, deadline)
    _validate_supported_state(
        first, baseline_head, baseline_branch, root, deadline
    )
    evidence = _capture_paths(
        root,
        baseline_head,
        first["head"],
        first["status"],
        baseline_files,
        max_size,
        deadline,
    )
    second = _git_state(root, deadline)
    _validate_supported_state(
        second, baseline_head, baseline_branch, root, deadline
    )
    second_evidence = _capture_paths(
        root,
        baseline_head,
        second["head"],
        second["status"],
        baseline_files,
        max_size,
        deadline,
    )
    if first != second or _evidence_identity(evidence) != _evidence_identity(
        second_evidence
    ):
        raise FinalizationError("FINAL_SNAPSHOT_UNSTABLE")

    return {**first, **evidence}


def _validate_supported_state(
    state: dict[str, Any],
    baseline_head: str,
    baseline_branch: str,
    root: Path,
    deadline: float,
) -> None:
    if state["branch"] != baseline_branch:
        raise FinalizationError("BRANCH_CHANGED_DURING_TASK")
    if state["head"] != baseline_head:
        _ensure_same_branch_advance(
            root, baseline_head, state["head"], deadline
        )


def _ensure_same_branch_advance(
    root: Path, baseline_head: str, final_head: str, deadline: float
) -> None:
    timeout = min(SUBPROCESS_TIMEOUT_SECONDS, deadline - time.monotonic())
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
    if result.returncode == 0:
        return
    if result.returncode == 1:
        raise FinalizationError("UNSUPPORTED_HEAD_STATE")
    raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE")


_REGULAR_BLOB_MODES = frozenset({"100644", "100755"})
_CLEAN_FINAL_STATUS = "  "


def _head_changed_paths(
    root: Path, baseline_head: str, final_head: str, deadline: float
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


def _git_state(root: Path, deadline: float) -> dict[str, Any]:
    try:
        head = _git(root, ["rev-parse", "--verify", "HEAD"], deadline)
        branch = _git(
            root,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            deadline,
        )
    except FinalizationError as error:
        if error.code == "FINAL_CAPTURE_FAILED":
            raise FinalizationError("UNSUPPORTED_HEAD_STATE") from error
        raise
    if not head or not branch:
        raise FinalizationError("UNSUPPORTED_HEAD_STATE")
    try:
        raw_index = _git(root, ["ls-files", "-s", "-z"], deadline)
        index = build_canonical_manifest(raw_index)
    except ValueError:
        raise FinalizationError("FINAL_CAPTURE_FAILED") from None
    return {
        "head": head.decode().strip(),
        "branch": branch.decode().strip(),
        "status": _git(
            root,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            deadline,
        ),
        "index": index,
    }


def _capture_paths(
    root: Path,
    baseline_head: str,
    final_head: str,
    final_status: bytes,
    baseline_files: list[BaselineFileRow],
    max_size: int,
    deadline: float,
) -> dict[str, list[Any]]:
    initial_rows = {row.path: row for row in baseline_files}
    final_states = _parse_status(final_status)
    committed = _head_changed_paths(root, baseline_head, final_head, deadline)
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
            )
            if initial is not None:
                frozen_baselines.append(initial)
        final = _read_final_head_file(
            root, final_head, path, max_size, deadline
        )
        if final_states.get(path) is not None:
            final = _read_final_file(
                root, path, final_states.get(path), max_size, deadline
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
    return {"baseline_files": frozen_baselines, "changes": changes}


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
    evidence: dict[str, list[Any]],
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    baselines = [
        (row.path, row.status, row.sha256, row.size)
        for row in evidence["baseline_files"]
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
        for row in evidence["changes"]
    ]
    return baselines, changes


def _read_head_file(
    root: Path,
    head: str,
    path: str,
    max_size: int,
    deadline: float,
) -> BaselineFileRow | None:
    try:
        tree_out = _git(root, ["ls-tree", "-z", head, "--", path], deadline)
    except FinalizationError as error:
        if error.code == "FINAL_SNAPSHOT_TIMEOUT":
            raise
        raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from error
    matched = _match_tree_entry(tree_out, path)
    if matched is None:
        return None
    _, kind = matched
    if kind == "tree":
        return None
    try:
        content = _git(root, ["show", f"{head}:{path}"], deadline)
    except FinalizationError as error:
        if error.code == "FINAL_SNAPSHOT_TIMEOUT":
            raise
        if error.code != "FINAL_CAPTURE_FAILED":
            raise
        raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from error
    return _file_row(path, "  ", content, max_size)


def _read_final_head_file(
    root: Path,
    head: str,
    path: str,
    max_size: int,
    deadline: float,
) -> BaselineFileRow | None:
    try:
        tree_out = _git(root, ["ls-tree", "-z", head, "--", path], deadline)
    except FinalizationError as error:
        if error.code == "FINAL_SNAPSHOT_TIMEOUT":
            raise
        raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from error
    matched = _match_tree_entry(tree_out, path)
    if matched is None:
        return None
    mode, kind = matched
    if kind == "tree":
        return None
    if kind != "blob" or mode not in _REGULAR_BLOB_MODES:
        raise FinalizationError("FINAL_SNAPSHOT_UNSTABLE")
    try:
        content = _git(root, ["show", f"{head}:{path}"], deadline)
    except FinalizationError as error:
        if error.code == "FINAL_SNAPSHOT_TIMEOUT":
            raise
        raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE") from error
    return _file_row(path, _CLEAN_FINAL_STATUS, content, max_size)


def _match_tree_entry(
    tree_out: bytes,
    path: str,
) -> tuple[str, str] | None:
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
        return mode, kind
    return None


def _read_final_file(
    root: Path,
    path: str,
    status: str | None,
    max_size: int,
    deadline: float,
) -> BaselineFileRow | None:
    if status is not None and "D" in status:
        return None
    target = root / path
    if status is None and not target.exists():
        return None
    try:
        if not target.is_file() or target.is_symlink():
            raise OSError
        content = target.read_bytes()
    except OSError as error:
        raise FinalizationError("FINAL_SNAPSHOT_UNSTABLE") from error
    if time.monotonic() > deadline:
        raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
    return _file_row(path, status or "  ", content, max_size)


def _file_row(
    path: str, status: str, content: bytes, max_size: int
) -> BaselineFileRow:
    binary = b"\0" in content
    return BaselineFileRow(
        path=path,
        status=status,
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        is_binary=int(binary),
        content=(
            gzip.compress(content)
            if not binary and len(content) <= max_size
            else None
        ),
    )


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


def _git(root: Path, arguments: list[str], deadline: float) -> bytes:
    timeout = min(SUBPROCESS_TIMEOUT_SECONDS, deadline - time.monotonic())
    if timeout <= 0:
        raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
    try:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            timeout=timeout,
        ).stdout
    except subprocess.TimeoutExpired as error:
        raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT") from error
    except subprocess.CalledProcessError as error:
        raise FinalizationError("FINAL_CAPTURE_FAILED") from error
