from __future__ import annotations

import gzip
import hashlib
import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from crucible_core.schemas.git import HashedContent, StreamedContent

# Chunked content hashing shared by baseline and final capture.

CHUNK_SIZE_BYTES = 65536


class HashBudget:
    """Mutable aggregate hashing budget for one capture operation.

    One instance lives for the whole ``capture_baseline``/``capture_final``
    call, spanning both stability reads and any retry, so aggregate
    exhaustion cannot be reset by re-reading.
    """

    def __init__(
        self, aggregate_budget: float, per_file_budget: float
    ) -> None:
        self.aggregate_budget = aggregate_budget
        self.per_file_budget = per_file_budget
        self.spent = 0.0

    def check(
        self,
        *,
        now: float,
        file_started_at: float,
        deadline: float,
        deadline_error: Callable[[], Exception],
        budget_error: Callable[[], Exception],
    ) -> None:
        """Enforce absolute deadline first, then per-file/aggregate."""
        if now >= deadline:
            raise deadline_error()
        elapsed = now - file_started_at
        if elapsed > self.per_file_budget:
            raise budget_error()
        if self.spent + elapsed > self.aggregate_budget:
            raise budget_error()

    def commit(self, elapsed: float) -> None:
        self.spent += elapsed


def hash_worktree_file(
    target: Path,
    max_size: int,
    *,
    tick: Callable[[], float],
    open_fn: Callable[..., Any],
    budget: HashBudget,
    file_started_at: float,
    deadline: float,
    deadline_error: Callable[[], Exception],
    budget_error: Callable[[], Exception],
) -> HashedContent:
    """Stream one worktree file, enforcing deadline and hash budgets.

    Reads in fixed-size chunks, updates SHA-256 incrementally, and
    retains at most ``max_size + 1`` bytes to decide snapshot storage.
    Charges elapsed hashing time into ``budget`` so the aggregate spans
    the whole capture operation. Each ``handle.read`` runs with a
    wall-clock timeout bounded by the remaining deadline/hash budget
    via a daemon thread (same mechanism as :func:`hash_stream`), so a
    blocked filesystem/FIFO cannot hang the caller indefinitely; on
    timeout the tightest bound decides between ``deadline_error()``
    and ``budget_error()``. Raises ``deadline_error()`` when the
    absolute deadline passes (checked again after retention/compression)
    and ``budget_error()`` on per-file or aggregate hash exhaustion.
    ``OSError`` from the handle propagates unchanged so the caller can
    charge elapsed once and promote deadline/hash exhaustion before
    mapping the remainder.
    """
    hasher = hashlib.sha256()
    size = 0
    binary = False
    retained = bytearray()
    with open_fn(target, "rb") as handle:
        last_now = file_started_at
        while True:
            deadline_remaining = deadline - last_now
            elapsed = last_now - file_started_at
            file_remaining = budget.per_file_budget - elapsed
            aggregate_remaining = budget.aggregate_budget - (
                budget.spent + elapsed
            )
            remaining = min(
                deadline_remaining, file_remaining, aggregate_remaining
            )
            if remaining <= 0:
                budget.check(
                    now=last_now,
                    file_started_at=file_started_at,
                    deadline=deadline,
                    deadline_error=deadline_error,
                    budget_error=budget_error,
                )
                if deadline_remaining <= min(
                    file_remaining, aggregate_remaining
                ):
                    raise deadline_error()
                raise budget_error()
            try:
                chunk = _read_chunk(handle, CHUNK_SIZE_BYTES, remaining)
            except TimeoutError as error:
                if deadline_remaining <= min(
                    file_remaining, aggregate_remaining
                ):
                    raise deadline_error() from error
                raise budget_error() from error
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
            if not binary and b"\0" in chunk:
                binary = True
            if len(retained) <= max_size:
                retained.extend(chunk[: max_size + 1 - len(retained)])
            last_now = tick()
            budget.check(
                now=last_now,
                file_started_at=file_started_at,
                deadline=deadline,
                deadline_error=deadline_error,
                budget_error=budget_error,
            )
    now = tick()
    budget.check(
        now=now,
        file_started_at=file_started_at,
        deadline=deadline,
        deadline_error=deadline_error,
        budget_error=budget_error,
    )
    budget.commit(now - file_started_at)
    saved = None
    if not binary and size <= max_size:
        saved = gzip.compress(bytes(retained))
    if tick() >= deadline:
        raise deadline_error()
    return HashedContent(
        sha256=hasher.hexdigest(), size=size, is_binary=int(binary), data=saved
    )


def hash_stream(
    stream: Any,
    max_size: int,
    *,
    tick: Callable[[], float],
    budget: HashBudget,
    file_started_at: float,
    deadline: float,
    deadline_error: Callable[[], Exception],
    budget_error: Callable[[], Exception],
    subprocess_timeout: float | None = None,
) -> StreamedContent:
    """Hash a binary stream in chunks without buffering the whole.

    Reads ``stream`` via ``read(CHUNK_SIZE_BYTES)`` until empty,
    updates SHA-256 incrementally and retains at most ``max_size + 1``
    bytes to decide snapshot storage. Enforces the absolute deadline
    and hash budgets on every chunk via ``budget``. Optionally
    enforces ``subprocess_timeout`` (elapsed since ``file_started_at``)
    reusing the same clock reading so streaming ``git show`` output
    cannot outlive the subprocess budget. Each ``read`` runs with a
    wall-clock timeout bounded by the remaining deadline/subprocess
    and per-file/aggregate hash budgets via a daemon thread, so a
    blocked producer cannot hang the caller indefinitely; on timeout
    the tightest bound decides between ``deadline_error()`` and
    ``budget_error()`` and the caller must kill/reap the producer.
    Performs checks only and
    returns the final clock reading; the caller commits ``budget``
    after verifying the producer succeeded, so failures never
    charge a partial hash twice. ``OSError`` from the stream
    propagates unchanged so the caller can charge elapsed once and
    promote deadline/hash exhaustion before mapping the remainder.
    """
    hasher = hashlib.sha256()
    size = 0
    binary = False
    retained = bytearray()
    last_now = file_started_at
    while True:
        deadline_remaining = deadline - last_now
        elapsed = last_now - file_started_at
        file_remaining = budget.per_file_budget - elapsed
        aggregate_remaining = budget.aggregate_budget - (
            budget.spent + elapsed
        )
        if subprocess_timeout is not None:
            subprocess_remaining: float | None = subprocess_timeout - elapsed
            deadline_like = min(deadline_remaining, subprocess_remaining)
        else:
            deadline_like = deadline_remaining
        budget_like = min(file_remaining, aggregate_remaining)
        remaining = min(deadline_like, budget_like)
        if remaining <= 0:
            if deadline_like <= budget_like:
                raise deadline_error()
            raise budget_error()
        try:
            chunk = _read_chunk(stream, CHUNK_SIZE_BYTES, remaining)
        except TimeoutError as error:
            if deadline_like <= budget_like:
                raise deadline_error() from error
            raise budget_error() from error
        if not chunk:
            break
        hasher.update(chunk)
        size += len(chunk)
        if not binary and b"\0" in chunk:
            binary = True
        if len(retained) <= max_size:
            retained.extend(chunk[: max_size + 1 - len(retained)])
        last_now = tick()
        if (
            subprocess_timeout is not None
            and last_now - file_started_at > subprocess_timeout
        ):
            raise deadline_error()
        budget.check(
            now=last_now,
            file_started_at=file_started_at,
            deadline=deadline,
            deadline_error=deadline_error,
            budget_error=budget_error,
        )
    now = tick()
    if (
        subprocess_timeout is not None
        and now - file_started_at > subprocess_timeout
    ):
        raise deadline_error()
    budget.check(
        now=now,
        file_started_at=file_started_at,
        deadline=deadline,
        deadline_error=deadline_error,
        budget_error=budget_error,
    )
    return StreamedContent(
        content=HashedContent(
            sha256=hasher.hexdigest(),
            size=size,
            is_binary=int(binary),
            data=retained,
        ),
        finished_at=now,
    )


def _read_chunk(stream: Any, size: int, timeout: float) -> bytes:
    """Read one chunk without blocking longer than ``timeout``.

    Runs the blocking ``stream.read`` on a daemon thread and waits at
    most ``timeout`` wall-clock seconds. On timeout raises
    ``TimeoutError`` and returns immediately without joining the
    daemon thread, so a blocked producer never stalls the caller;
    the caller must kill/reap the producer, which unblocks the
    abandoned read. ``OSError`` from the stream is re-raised.
    """
    results: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def _target() -> None:
        try:
            results.put(("ok", stream.read(size)))
        except BaseException as error:  # noqa: BLE001 - re-raised below
            results.put(("err", error))

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    try:
        kind, payload = results.get(timeout=max(timeout, 0.001))
    except queue.Empty as error:
        raise TimeoutError("stream read timed out") from error
    if kind == "err":
        raise payload
    return payload
