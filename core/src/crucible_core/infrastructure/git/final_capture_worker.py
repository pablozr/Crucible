from __future__ import annotations

import multiprocessing
import multiprocessing.connection
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from crucible_core.core.errors import FinalizationError
from crucible_core.schemas.finalizations import (
    FinalCaptureEnvelope,
    FinalCaptureRequest,
    FinalCaptureSnapshot,
)

BOUNDARY_LOCK: threading.RLock = threading.RLock()

_CTX = multiprocessing.get_context("spawn")

# Internal transport-failure code raised by wait_capture ONLY for
# broken IPC (poll/recv EOF on a killed/cancelled child, invalid
# envelope payload). Never raised for envelope ok=False: genuine
# child error codes (including a genuine FINALIZATION_FAILED or a
# child-side FINAL_SNAPSHOT_TIMEOUT) travel untouched in the
# envelope. The coordinator maps this internal code to STALE when
# fenced, to FINALIZATION_FAILED/500 otherwise, so it never leaks as
# a public contract.
IPC_FAILED_CODE = "FINAL_CAPTURE_WORKER_IPC_FAILED"

# Internal parent-wait-timeout code raised by wait_capture ONLY when
# the parent itself observes its own deadline expire (expired
# deadline on entry, poll timeout, recv failure past the deadline,
# valid envelope arriving past the deadline). Never raised for a
# child-side FINAL_SNAPSHOT_TIMEOUT arriving inside a genuine
# envelope. The coordinator maps this internal code to STALE when
# fenced, to public FINAL_SNAPSHOT_TIMEOUT otherwise, so it never
# leaks as a public contract and never mixes with envelope timeout.
WAIT_TIMEOUT_CODE = "FINAL_CAPTURE_WORKER_WAIT_TIMEOUT"

CaptureKey = tuple[str, int]


@dataclass
class _RunningCapture:
    tree_id: str
    generation: int
    task_id: str
    process: Any
    conn: Any


_REGISTRY: dict[CaptureKey, _RunningCapture] = {}


def final_capture_worker_main(
    child_conn: Any, request: FinalCaptureRequest
) -> None:
    """Top-level spawn target (importable). Git/worktree reads only.

    Never receives DB path/connection/callback. Sets
    GIT_OPTIONAL_LOCKS=0 and returns a snapshot/error envelope over IPC.
    """
    try:
        os.environ["GIT_OPTIONAL_LOCKS"] = "0"
        # Import locally so spawn re-import stays light and never pulls
        # DB/application layers into the child.
        from crucible_core.infrastructure.git.final_capture import (
            capture_final,
        )

        snapshot = capture_final(
            Path(request.git_root),
            request.baseline_head,
            request.baseline_branch,
            request.baseline_index,
            list(request.baseline_files),
            request.max_file_size_bytes,
            request.deadline_monotonic,
        )
        envelope = FinalCaptureEnvelope(ok=True, snapshot=snapshot)
        try:
            child_conn.send(envelope)
        except Exception:
            pass
    except FinalizationError as error:
        try:
            child_conn.send(
                FinalCaptureEnvelope(
                    ok=False,
                    error_code=error.code,
                    error_status=error.status_code,
                )
            )
        except Exception:
            pass
    except Exception:
        try:
            child_conn.send(
                FinalCaptureEnvelope(
                    ok=False,
                    error_code="FINALIZATION_FAILED",
                    error_status=500,
                )
            )
        except Exception:
            pass
    finally:
        try:
            child_conn.close()
        except Exception:
            pass


def spawn_capture(
    request: FinalCaptureRequest,
    tree_id: str,
    generation: int,
    task_id: str,
) -> CaptureKey:
    """Reserve (tree, generation) and start the child atomically.

    Registry insertion and ``Process.start`` run inside a single
    ``BOUNDARY_LOCK`` critical section, so a fence racing on another
    thread can never cancel/remove an unstarted handle and observe a
    later ``start``: either the worker is fully started before the
    fence snapshots it (and is then cancelled), or the fence snapshot
    lands first and the worker belongs to a newer generation begun
    afterwards. A ``start`` failure removes only our own entry and
    closes both pipe ends without ever starting the child after a
    cancel. Callers must NOT hold the lock across wait (Git capture).
    """
    key: CaptureKey = (tree_id, generation)
    parent_conn, child_conn = _CTX.Pipe(duplex=False)
    process = _CTX.Process(
        target=final_capture_worker_main,
        args=(child_conn, request),
    )
    with BOUNDARY_LOCK:
        _REGISTRY[key] = _RunningCapture(
            tree_id=tree_id,
            generation=generation,
            task_id=task_id,
            process=process,
            conn=parent_conn,
        )
        try:
            process.start()
        except Exception:
            current = _REGISTRY.get(key)
            if current is not None and current.process is process:
                _REGISTRY.pop(key, None)
            try:
                parent_conn.close()
            except Exception:
                pass
            try:
                child_conn.close()
            except Exception:
                pass
            raise
        try:
            child_conn.close()
        except Exception:
            pass
        return key


def snapshot_tree_keys(tree_id: str) -> list[CaptureKey]:
    with BOUNDARY_LOCK:
        return [key for key in _REGISTRY if key[0] == tree_id]


def _terminate_and_join(process: Any) -> None:
    try:
        if not process.is_alive():
            try:
                process.join(timeout=1)
            except Exception:
                pass
            return
        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.join(timeout=1)
        except Exception:
            pass
        if process.is_alive():
            try:
                process.kill()
            except Exception:
                pass
            try:
                process.join(timeout=1)
            except Exception:
                pass
    except Exception:
        pass


def wait_capture(
    key: CaptureKey,
    deadline: float,
    monotonic: Callable[[], float] | None = None,
) -> FinalCaptureSnapshot:
    """Wait for snapshot within deadline (spawn+capture+IPC included).

    Parent-observed deadline expiry raises the internal
    WAIT_TIMEOUT_CODE; broken IPC (recv EOF on a killed/cancelled
    child, invalid envelope payload) raises the internal
    IPC_FAILED_CODE -- never a genuine envelope error code, not even
    a child-side FINAL_SNAPSHOT_TIMEOUT, which travels untouched in
    the envelope. The late result, if any, is discarded;
    publication_is_current/fence in SQLite remains the publication
    authority.
    """
    tick = monotonic or time.monotonic
    with BOUNDARY_LOCK:
        handle = _REGISTRY.get(key)
    if handle is None:
        raise FinalizationError("STALE_CAPTURE_GENERATION", 409)
    try:
        remaining = deadline - tick()
        if remaining <= 0:
            _terminate_and_join(handle.process)
            raise FinalizationError(WAIT_TIMEOUT_CODE)
        try:
            ready = handle.conn.poll(remaining)
        except Exception as error:
            _terminate_and_join(handle.process)
            raise FinalizationError(IPC_FAILED_CODE, 500) from error
        if not ready:
            _terminate_and_join(handle.process)
            raise FinalizationError(WAIT_TIMEOUT_CODE)
        try:
            envelope = handle.conn.recv()
        except Exception as error:
            _terminate_and_join(handle.process)
            if tick() >= deadline:
                raise FinalizationError(WAIT_TIMEOUT_CODE) from error
            raise FinalizationError(IPC_FAILED_CODE, 500) from error
        try:
            handle.process.join(timeout=1)
        except Exception:
            pass
        if handle.process.is_alive():
            _terminate_and_join(handle.process)
        if not isinstance(envelope, FinalCaptureEnvelope):
            raise FinalizationError(IPC_FAILED_CODE, 500)
        if not envelope.ok or envelope.snapshot is None:
            raise FinalizationError(
                envelope.error_code or "FINALIZATION_FAILED",
                envelope.error_status or 400,
            )
        if tick() >= deadline:
            raise FinalizationError(WAIT_TIMEOUT_CODE)
        return envelope.snapshot
    finally:
        with BOUNDARY_LOCK:
            _REGISTRY.pop(key, None)
        try:
            handle.conn.close()
        except Exception:
            pass
        try:
            handle.process.join(timeout=1)
        except Exception:
            pass


def cancel_capture(key: CaptureKey) -> bool:
    """Best-effort terminate/kill/join + reap. Never raises."""
    with BOUNDARY_LOCK:
        handle = _REGISTRY.pop(key, None)
    if handle is None:
        return False
    try:
        _terminate_and_join(handle.process)
    finally:
        try:
            handle.conn.close()
        except Exception:
            pass
    return True


def cancel_tree_captures(tree_id: str) -> int:
    """Cancel/reap every worker for a tree. DB fence stays authoritative."""
    with BOUNDARY_LOCK:
        keys = [key for key in _REGISTRY if key[0] == tree_id]
    count = 0
    for key in keys:
        try:
            if cancel_capture(key):
                count += 1
        except Exception:
            continue
    return count


def clear_registry_for_tests() -> None:
    with BOUNDARY_LOCK:
        _REGISTRY.clear()
