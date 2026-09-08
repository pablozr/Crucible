from __future__ import annotations

import gzip
import hashlib
import subprocess
import time
from pathlib import Path
from typing import Any

from crucible_core.infrastructure.git.index_manifest import (
    build_canonical_manifest,
)
from crucible_core.logging import get_logger
from crucible_core.schemas.persistence import BaselineFileRow

SUBPROCESS_TIMEOUT_SECONDS = 0.5

logger = get_logger(__name__)


def capture_baseline(
    root: Path,
    max_size: int,
    deadline: float,
) -> dict[str, Any]:
    from crucible_core.core.errors import AdmissionError

    for _ in range(2):
        try:
            first = _git_state(root, deadline)
            files = _snapshot_files(root, first["status"], max_size, deadline)
            second = _git_state(root, deadline)
            second_files = _snapshot_files(
                root, second["status"], max_size, deadline
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


def _git_state(root: Path, deadline: float) -> dict[str, Any]:
    from crucible_core.core.errors import AdmissionError

    try:
        head = _git(root, ["rev-parse", "--verify", "HEAD"], deadline)
        branch = _git(
            root,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            deadline,
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
        raw_index = _git(root, ["ls-files", "-s", "-z"], deadline)
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
        ),
        "index": index,
    }


def _git(root: Path, arguments: list[str], deadline: float) -> bytes:
    from crucible_core.core.errors import AdmissionError

    timeout = min(SUBPROCESS_TIMEOUT_SECONDS, deadline - time.monotonic())
    if timeout <= 0:
        raise AdmissionError("BASELINE_CAPTURE_TIMEOUT")
    try:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            timeout=timeout,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        logger.warning("git command failed code=BASELINE_CAPTURE_FAILED")
        raise AdmissionError("BASELINE_CAPTURE_FAILED") from error


def _snapshot_files(
    root: Path, status: bytes, max_size: int, deadline: float
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

        try:
            if not target.is_file() or target.is_symlink():
                raise OSError
            hash_started_at = time.monotonic()
            content = target.read_bytes()
        except OSError:
            logger.warning("baseline file read failed code=BASELINE_UNSTABLE")
            raise AdmissionError("BASELINE_UNSTABLE") from None

        if time.monotonic() > deadline:
            raise AdmissionError("BASELINE_CAPTURE_TIMEOUT")

        digest = hashlib.sha256(content).hexdigest()

        if time.monotonic() - hash_started_at > SUBPROCESS_TIMEOUT_SECONDS:
            raise AdmissionError("BASELINE_HASH_TIMEOUT")

        binary = b"\0" in content
        saved = None

        if not binary and len(content) <= max_size:
            saved = gzip.compress(content)

        rows.append(
            BaselineFileRow(
                path=str(path),
                status=xy,
                sha256=digest,
                size=len(content),
                is_binary=int(binary),
                content=saved,
            )
        )
    return rows


def _file_identity(
    files: list[BaselineFileRow],
) -> list[tuple[str, str, str | None, int | None]]:
    return [(item.path, item.status, item.sha256, item.size) for item in files]
