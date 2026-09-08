from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from crucible_core.infrastructure.git.content_hash import (
    HashBudget,
    hash_worktree_file,
)
from crucible_core.infrastructure.git.index_manifest import (
    build_canonical_manifest,
)
from crucible_core.logging import get_logger
from crucible_core.schemas.persistence import BaselineFileRow

SUBPROCESS_TIMEOUT_SECONDS = 0.5
HASH_PER_FILE_BUDGET_SECONDS = 0.5
HASH_AGGREGATE_BUDGET_SECONDS = 0.75

logger = get_logger(__name__)


def capture_baseline(
    root: Path,
    max_size: int,
    deadline: float,
    *,
    clock: Callable[[], float] | None = None,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    from crucible_core.core.errors import AdmissionError

    tick = clock or time.monotonic
    open_fn = opener or open
    budget = HashBudget(
        aggregate_budget=HASH_AGGREGATE_BUDGET_SECONDS,
        per_file_budget=HASH_PER_FILE_BUDGET_SECONDS,
    )
    for _ in range(2):
        try:
            first = _git_state(root, deadline, tick)
            files = _snapshot_files(
                root,
                first["status"],
                max_size,
                deadline,
                tick,
                open_fn,
                budget,
            )
            second = _git_state(root, deadline, tick)
            second_files = _snapshot_files(
                root,
                second["status"],
                max_size,
                deadline,
                tick,
                open_fn,
                budget,
            )
        except AdmissionError as error:
            if error.code == "BASELINE_UNSTABLE":
                logger.warning("baseline capture retry code=BASELINE_UNSTABLE")
                continue
            logger.warning("baseline capture failed code=%s", error.code)
            raise

        if first == second and _file_identity(files) == _file_identity(
            second_files
        ):
            first["files"] = files
            return first
    raise AdmissionError("BASELINE_UNSTABLE")


def _git_state(
    root: Path, deadline: float, tick: Callable[[], float]
) -> dict[str, Any]:
    from crucible_core.core.errors import AdmissionError

    try:
        head = _git(root, ["rev-parse", "--verify", "HEAD"], deadline, tick)
        branch = _git(
            root,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            deadline,
            tick,
        )
    except AdmissionError as error:
        if error.code == "BASELINE_CAPTURE_FAILED":
            logger.warning("git state failed code=UNSUPPORTED_HEAD_STATE")
            raise AdmissionError("UNSUPPORTED_HEAD_STATE") from error
        logger.warning("git state failed code=%s", error.code)
        raise

    if not head or not branch:
        raise AdmissionError("UNSUPPORTED_HEAD_STATE")

    try:
        raw_index = _git(root, ["ls-files", "-s", "-z"], deadline, tick)
        index = build_canonical_manifest(raw_index)
    except ValueError:
        logger.warning("git index failed code=BASELINE_CAPTURE_FAILED")
        raise AdmissionError("BASELINE_CAPTURE_FAILED") from None

    return {
        "head": head.decode().strip(),
        "branch": branch.decode().strip(),
        "status": _git(
            root,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            deadline,
            tick,
        ),
        "index": index,
    }


def _git(
    root: Path,
    arguments: list[str],
    deadline: float,
    tick: Callable[[], float],
) -> bytes:
    from crucible_core.core.errors import AdmissionError

    timeout = min(SUBPROCESS_TIMEOUT_SECONDS, deadline - tick())
    if timeout <= 0:
        raise AdmissionError("BASELINE_CAPTURE_TIMEOUT")
    try:
        stdout = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            timeout=timeout,
        ).stdout
    except subprocess.TimeoutExpired as error:
        logger.warning("git command failed code=BASELINE_CAPTURE_TIMEOUT")
        raise AdmissionError("BASELINE_CAPTURE_TIMEOUT") from error
    except (subprocess.CalledProcessError, OSError) as error:
        if tick() >= deadline:
            raise AdmissionError("BASELINE_CAPTURE_TIMEOUT") from error
        logger.warning("git command failed code=BASELINE_CAPTURE_FAILED")
        raise AdmissionError("BASELINE_CAPTURE_FAILED") from error
    if tick() >= deadline:
        raise AdmissionError("BASELINE_CAPTURE_TIMEOUT")
    return stdout


def _snapshot_files(
    root: Path,
    status: bytes,
    max_size: int,
    deadline: float,
    tick: Callable[[], float],
    open_fn: Callable[..., Any],
    budget: HashBudget,
) -> list[BaselineFileRow]:
    from crucible_core.core.errors import AdmissionError

    rows: list[BaselineFileRow] = []
    for entry in status.split(b"\0"):
        if not entry:
            continue

        xy, raw_path = entry[:2].decode("ascii"), entry[3:]

        if "R" in xy or "C" in xy:
            raise AdmissionError("UNSUPPORTED_BASELINE_PATH")

        path = Path(raw_path.decode("utf-8", "surrogateescape"))
        target = root / path

        deleted = "D" in xy
        if deleted:
            rows.append(BaselineFileRow(path=str(path), status=xy))
            continue

        file_started_at = tick()
        try:
            if not target.is_file() or target.is_symlink():
                raise OSError
            digest, size, binary, saved = _hash_worktree_file(
                target,
                max_size,
                deadline,
                tick,
                open_fn,
                budget,
                file_started_at,
            )
        except OSError:
            elapsed = tick() - file_started_at
            budget.commit(elapsed)
            if (
                elapsed > budget.per_file_budget
                or budget.spent > budget.aggregate_budget
            ):
                raise AdmissionError("BASELINE_HASH_TIMEOUT") from None
            logger.warning("baseline file read failed code=BASELINE_UNSTABLE")
            raise AdmissionError("BASELINE_UNSTABLE") from None

        if tick() >= deadline:
            raise AdmissionError("BASELINE_CAPTURE_TIMEOUT")

        rows.append(
            BaselineFileRow(
                path=str(path),
                status=xy,
                sha256=digest,
                size=size,
                is_binary=binary,
                content=saved,
            )
        )
    return rows


def _hash_worktree_file(
    target: Path,
    max_size: int,
    deadline: float,
    tick: Callable[[], float],
    open_fn: Callable[..., Any],
    budget: HashBudget,
    file_started_at: float,
) -> tuple[str, int, int, bytes | None]:
    from crucible_core.core.errors import AdmissionError

    return hash_worktree_file(
        target,
        max_size,
        tick=tick,
        open_fn=open_fn,
        budget=budget,
        file_started_at=file_started_at,
        deadline=deadline,
        deadline_error=lambda: AdmissionError("BASELINE_CAPTURE_TIMEOUT"),
        budget_error=lambda: AdmissionError("BASELINE_HASH_TIMEOUT"),
    )


def _file_identity(
    files: list[BaselineFileRow],
) -> list[tuple[str, str, str | None, int | None]]:
    return [(item.path, item.status, item.sha256, item.size) for item in files]
