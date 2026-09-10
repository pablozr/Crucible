from __future__ import annotations

import difflib
import gzip
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from crucible_core.core.database import connect
from crucible_core.core.errors import FinalizationError, ProjectError
from crucible_core.infrastructure.git import final_capture_worker as worker
from crucible_core.repositories import finalizations_repository as final_repo
from crucible_core.repositories import sessions_repository as sessions_repo
from crucible_core.repositories import tasks_repository as tasks_repo
from crucible_core.schemas.admissions import EventRequest
from crucible_core.schemas.finalizations import (
    BeginFinalizationResult,
    BeginReplayed,
    FinalCaptureRequest,
    FinalCaptureSnapshot,
)
from crucible_core.schemas.persistence import (
    BaselineFileRow,
    FinalizationEventRow,
    FinalizationTask,
    TaskFileChangeRow,
)
from crucible_core.services.projects import resolve_project
from crucible_core.utils.functions import canonical_json_sha256, utc_now_iso

FINAL_CAPTURE_DEADLINE_SECONDS = 5
SUPPORTED_ADAPTER = "opencode-v1"
SUPPORTED_ADAPTER_VERSION = "0.1.0"
SUPPORTED_PROFILE = "opencode-v1-1.18.28-write-stop-restricted"
SUPPORTED_SIGNAL = "session_prompt_return"
SUPPORTED_OUTCOME = "stop"
SUPPORTED_ABORT_EVENT = "task_finalization_aborted"
SUPPORTED_ABORT_REASONS = frozenset(
    {
        "DISPATCH_FAILED",
        "TERMINAL_OBSERVER_FAILED",
        "TERMINAL_SIGNAL_MISMATCH",
    }
)

_RETRYABLE_CONFLICT_CODES = frozenset(
    {
        "IDEMPOTENCY_CONFLICT",
        "INPUT_TASK_MISMATCH",
        "STALE_CAPTURE_GENERATION",
        "TASK_CORRELATION_MISMATCH",
        "TASK_NOT_RUNNING",
        "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT",
        "FINALIZATION_IN_PROGRESS",
        "EXECUTION_ID_MISMATCH",
    }
)

_SERVER_FAILURE_CODES = frozenset(
    {
        "FINALIZATION_FAILED",
        "FINAL_MATERIALIZATION_FAILED",
        "FINAL_SNAPSHOT_NOT_FROZEN",
        "TERMINAL_AUTHORIZATION_UNCONFIGURED",
    }
)

# Parent-side worker failures superseded by a durable fence.
# worker.WAIT_TIMEOUT_CODE is what wait_capture raises ONLY when the
# parent itself observes its own 5s budget (spawn+capture+IPC)
# expire. worker.IPC_FAILED_CODE is what wait_capture raises ONLY
# for broken IPC (poll/recv EOF on a killed/cancelled child, invalid
# envelope payload) -- never for envelope ok=False. A child-side
# FINAL_SNAPSHOT_TIMEOUT arriving inside a genuine envelope is NOT
# converted: it stays FINAL_SNAPSHOT_TIMEOUT after a fence, like any
# other specific capture_final code (including a genuine envelope
# FINALIZATION_FAILED), so pre-fence capture failures are never
# masked.
_FENCE_SUPERSEDED_WORKER_CODES = frozenset(
    {
        worker.IPC_FAILED_CODE,
        worker.WAIT_TIMEOUT_CODE,
    }
)


def _replay_status(code: str) -> int:
    if code == "TASK_NOT_FOUND":
        return 404
    if code == "CAPTURE_AUTHORIZATION_EXPIRED":
        return 410
    if code in _RETRYABLE_CONFLICT_CODES:
        return 409
    if code in _SERVER_FAILURE_CODES:
        return 500
    return 400


_ALLOWED_PARTIAL_EVIDENCE = frozenset({"complete", "hash_only", "unsupported"})


def _evidence_completeness(changes: list[TaskFileChangeRow]) -> str:
    # Honest Task completeness: completed+complete only when every
    # change is complete; completed+partial for structural/hash-only
    # honestly captured; unavailable forces failure before freeze.
    if any(row.evidence_status == "unavailable" for row in changes):
        return "unavailable"
    if all(row.evidence_status == "complete" for row in changes):
        return "complete"
    if all(
        row.evidence_status in _ALLOWED_PARTIAL_EVIDENCE for row in changes
    ):
        return "partial"
    return "unavailable"


def _parse_utc_timestamp(value: object, code: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise FinalizationError(code)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise FinalizationError(code) from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise FinalizationError(code)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class EventHashContext:
    event: EventRequest
    payload_hash: str


def _is_transient_lock(error: Exception) -> bool:
    return isinstance(error, sqlite3.OperationalError) and (
        "locked" in str(error).lower() or "busy" in str(error).lower()
    )


class FinalizationCoordinator:
    def __init__(
        self,
        database_path: Path,
        *,
        capture_runner: worker.CaptureRunner | None = None,
        publication_hook: Callable[[], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        max_authorization_window_seconds: int | None = None,
        monotonic: Callable[[], float] | None = None,
        patch_hook: Callable[[], None] | None = None,
    ) -> None:
        self._database_path = database_path
        self._runner = capture_runner or worker.get_default_runner()
        self._publication_hook = publication_hook
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_window_seconds = max_authorization_window_seconds
        self._monotonic = monotonic or time.monotonic
        self._patch_hook = patch_hook

    def complete(
        self, event: EventRequest, payload_hash: str | None = None
    ) -> dict[str, object]:
        event_id = str(event.event_id)
        if payload_hash is None:
            payload_hash = canonical_json_sha256(
                event.model_dump(mode="json", exclude_none=True)
            )
        self._validate_event(event)

        with connect(self._database_path) as connection:
            existing = final_repo.get_finalization_event(connection, event_id)
            if existing is not None:
                return self._reconcile(event_id, payload_hash, existing, event)

        task_id = self._task_id(event)
        not_after = _parse_utc_timestamp(
            event.payload.get("capture_not_after"),
            "INVALID_CAPTURE_NOT_AFTER",
        )

        with connect(self._database_path) as connection:
            persisted = tasks_repo.get_finalization_task(connection, task_id)
            if persisted is None:
                raise FinalizationError("TASK_NOT_FOUND", 404)
            self._validate_correlation(connection, event, persisted)
            if persisted.status != "running":
                raise FinalizationError("TASK_NOT_RUNNING", 409)
            linked = tasks_repo.find_input(
                connection, persisted.session_id, event.input_id
            )
            if linked is None or linked.task_id != task_id:
                raise FinalizationError("INPUT_TASK_MISMATCH", 409)

        if self._clock() > not_after:
            return self._abort_expired(event, task_id, payload_hash)

        deadline = self._monotonic() + FINAL_CAPTURE_DEADLINE_SECONDS
        runner = self._runner
        key: Any = None
        # Single begin -> prepare -> start path for real and fake
        # runners. Begin, project/baseline preparation and registry
        # start stay atomic under the shared boundary lock; the
        # lock is never held during Git capture (wait).
        _pre_generation: int | None = None
        try:
            with runner.boundary_lock:
                try:
                    begun = self._begin(
                        event, task_id, payload_hash, not_after
                    )
                except sqlite3.IntegrityError:
                    with connect(self._database_path) as connection:
                        existing = final_repo.get_finalization_event(
                            connection, event_id
                        )
                        if existing is None:
                            raise
                        return self._reconcile(
                            event_id, payload_hash, existing, event
                        )
                if isinstance(begun, BeginReplayed):
                    return begun.response
                generation, task, input_row_id = (
                    begun.generation,
                    begun.task,
                    begun.input_row_id,
                )
                _pre_generation = int(generation)
                try:
                    project = resolve_project(event.git_root)
                except ProjectError as error:
                    raise FinalizationError(str(error)) from error
                if project.id != str(event.project_id):
                    raise FinalizationError("PROJECT_ID_MISMATCH")
                with connect(self._database_path) as connection:
                    baseline_files = tasks_repo.list_task_baseline_files(
                        connection, task_id
                    )
                # Child enforces its own absolute deadline on its
                # own monotonic clock; translate the remaining
                # parent budget so injected parent clocks stay
                # deterministic.
                remaining = max(0.001, deadline - self._monotonic())
                request = FinalCaptureRequest(
                    git_root=str(task.git_root),
                    baseline_head=str(task.baseline_head),
                    baseline_branch=str(task.baseline_branch),
                    baseline_index=bytes(task.baseline_index_manifest),
                    baseline_files=list(baseline_files),
                    max_file_size_bytes=int(
                        project.max_snapshot_file_size_bytes
                    ),
                    deadline_monotonic=time.monotonic() + remaining,
                )
                key = runner.spawn_capture(
                    request,
                    str(task.tree_id),
                    int(generation),
                    task_id,
                )
        except FinalizationError as pre_error:
            if _pre_generation is not None:
                try:
                    self._fail(
                        task_id,
                        _pre_generation,
                        pre_error.code,
                    )
                except Exception:
                    pass
            raise
        except Exception:
            if _pre_generation is not None:
                try:
                    self._fail(
                        task_id,
                        _pre_generation,
                        "FINALIZATION_FAILED",
                    )
                except Exception:
                    pass
                raise FinalizationError("FINALIZATION_FAILED", 500) from None
            raise
        try:
            try:
                snapshot = runner.wait_capture(key, deadline, self._monotonic)
            except FinalizationError as wait_error:
                if (
                    wait_error.code in _FENCE_SUPERSEDED_WORKER_CODES
                    and self._was_fenced(
                        task_id, str(task.tree_id), int(generation)
                    )
                ):
                    raise FinalizationError(
                        "STALE_CAPTURE_GENERATION", 409
                    ) from wait_error
                raise
            if self._monotonic() >= deadline:
                raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
            if self._publication_hook is not None:
                self._publication_hook()
            if self._monotonic() >= deadline:
                raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
            # Only the parent freezes/materializes from SQLite
            # authority; a late runner result can never publish
            # once publication_is_current/fence rejects it.
            self._freeze(task_id, task.tree_id, generation, snapshot, deadline)
            self.materialize(task_id)
        except FinalizationError as error:
            if (
                error.code in _FENCE_SUPERSEDED_WORKER_CODES
                and self._was_fenced(
                    task_id, str(task.tree_id), int(generation)
                )
            ):
                self._fail(task_id, generation, "STALE_CAPTURE_GENERATION")
                raise FinalizationError(
                    "STALE_CAPTURE_GENERATION", 409
                ) from error
            if error.code == worker.IPC_FAILED_CODE:
                # Broken IPC without a fence: internal code never
                # leaks; map to the public worker-failure contract.
                self._fail(task_id, generation, "FINALIZATION_FAILED")
                raise FinalizationError("FINALIZATION_FAILED", 500) from error
            if error.code == worker.WAIT_TIMEOUT_CODE:
                # Parent-side timeout without a fence: internal
                # code never leaks; map to the public contract.
                self._fail(task_id, generation, "FINAL_SNAPSHOT_TIMEOUT")
                raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT") from error
            if _is_transient_lock(error):
                raise
            self._fail(task_id, generation, error.code)
            raise
        except Exception as error:
            if _is_transient_lock(error):
                # Writer/busy during freeze/materialize is
                # retryable: leave the task finalizing so
                # recovery can retry; never convert a lock
                # into a definitive corruption/failure.
                raise FinalizationError(
                    "FINALIZATION_IN_PROGRESS", 409
                ) from error
            try:
                self._fail(task_id, generation, "FINALIZATION_FAILED")
            except Exception:
                pass
            raise FinalizationError("FINALIZATION_FAILED", 500) from None

        return {
            "event_id": event_id,
            "status": "accepted",
            "outcome": "completed",
            "input_id": input_row_id,
            "task_id": task_id,
            "dispatch_authorized": False,
        }

    def abort(
        self, event: EventRequest, payload_hash: str | None = None
    ) -> dict[str, object]:
        event_id = str(event.event_id)
        if payload_hash is None:
            payload_hash = canonical_json_sha256(
                event.model_dump(mode="json", exclude_none=True)
            )
        self._validate_abort_event(event)

        with connect(self._database_path) as connection:
            existing = final_repo.get_finalization_event(connection, event_id)
            if existing is not None:
                return self._reconcile(event_id, payload_hash, existing, event)

        task_id = self._task_id(event)
        reason = self._abort_reason(event)

        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                duplicate = final_repo.get_finalization_event(
                    connection, event_id
                )
                if duplicate is not None:
                    connection.rollback()
                    return self._reconcile(
                        event_id, payload_hash, duplicate, event
                    )
                task = tasks_repo.get_finalization_task(connection, task_id)
                if task is None:
                    connection.rollback()
                    raise FinalizationError("TASK_NOT_FOUND", 404)
                self._validate_correlation(connection, event, task)
                if task.status != "running":
                    connection.rollback()
                    raise FinalizationError("TASK_NOT_RUNNING", 409)
                stored = tasks_repo.find_input(
                    connection, task.session_id, event.input_id
                )
                if stored is None or stored.task_id != task_id:
                    connection.rollback()
                    raise FinalizationError("INPUT_TASK_MISMATCH", 409)

                now = utc_now_iso()
                aborted = final_repo.abort_running_task_with_reason(
                    connection, task_id, reason, now
                )
                if not aborted:
                    connection.rollback()
                    raise FinalizationError("TASK_NOT_RUNNING", 409)
                final_repo.insert_abort_event(
                    connection,
                    event_id,
                    payload_hash,
                    now,
                    stored.id,
                    task_id,
                    reason,
                    self._legacy_hash(event),
                )
                connection.commit()
            except sqlite3.IntegrityError:
                connection.rollback()
                with connect(self._database_path) as retry_connection:
                    existing_retry = final_repo.get_finalization_event(
                        retry_connection, event_id
                    )
                    if existing_retry is None:
                        raise
                    return self._reconcile(
                        event_id, payload_hash, existing_retry, event
                    )
            return {
                "event_id": event_id,
                "status": "rejected",
                "outcome": "rejected",
                "input_id": stored.id,
                "task_id": task_id,
                "dispatch_authorized": False,
            }

    def _begin(
        self,
        event: EventRequest,
        task_id: str,
        payload_hash: str,
        not_after: datetime,
    ) -> BeginFinalizationResult | BeginReplayed:
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = final_repo.get_finalization_event(
                connection, str(event.event_id)
            )
            if duplicate is not None:
                connection.rollback()
                return BeginReplayed(
                    response=self._reconcile(
                        str(event.event_id),
                        payload_hash,
                        duplicate,
                        event,
                    )
                )
            task = tasks_repo.get_finalization_task(connection, task_id)
            if task is None:
                raise FinalizationError("TASK_NOT_FOUND", 404)
            self._validate_correlation(connection, event, task)
            if task.status != "running":
                raise FinalizationError("TASK_NOT_RUNNING", 409)
            stored = tasks_repo.find_input(
                connection, task.session_id, event.input_id
            )
            if stored is None or stored.task_id != task_id:
                raise FinalizationError("INPUT_TASK_MISMATCH", 409)

            if self._clock() > not_after:
                return BeginReplayed(
                    response=self._reject_expired_locked(
                        connection, event, task_id, payload_hash, stored.id
                    )
                )

            generation = final_repo.begin_finalization(
                connection,
                task_id,
                task.tree_id,
                event.execution_id or "",
                event.payload["terminal_signal"],
                event.payload["terminal_outcome"],
                event.payload["compatibility_profile"],
                str(event.payload["terminal_observed_at"]),
                str(event.payload["capture_not_after"]),
            )
            if generation is None:
                raise FinalizationError("STALE_CAPTURE_GENERATION", 409)
            final_repo.insert_processing_event(
                connection,
                str(event.event_id),
                payload_hash,
                "task_completed",
                utc_now_iso(),
                "finalizing",
                stored.id,
                task_id,
                self._legacy_hash(event),
            )
            connection.commit()
            return BeginFinalizationResult(
                generation=generation, task=task, input_row_id=stored.id
            )

    def _abort_expired(
        self,
        event: EventRequest,
        task_id: str,
        payload_hash: str,
    ) -> dict[str, object]:
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = final_repo.get_finalization_event(
                connection, str(event.event_id)
            )
            if duplicate is not None:
                connection.rollback()
                return self._reconcile(
                    str(event.event_id),
                    payload_hash,
                    duplicate,
                    event,
                )
            task = tasks_repo.get_finalization_task(connection, task_id)
            if task is None:
                connection.rollback()
                raise FinalizationError("TASK_NOT_FOUND", 404)
            self._validate_correlation(connection, event, task)
            if task.status != "running":
                connection.rollback()
                raise FinalizationError("TASK_NOT_RUNNING", 409)
            stored = tasks_repo.find_input(
                connection, task.session_id, event.input_id
            )
            if stored is None or stored.task_id != task_id:
                connection.rollback()
                raise FinalizationError("INPUT_TASK_MISMATCH", 409)

            return self._reject_expired_locked(
                connection, event, task_id, payload_hash, stored.id
            )

    def _reject_expired_locked(
        self,
        connection: Any,
        event: EventRequest,
        task_id: str,
        payload_hash: str,
        input_row_id: str,
    ) -> dict[str, object]:
        code = "CAPTURE_AUTHORIZATION_EXPIRED"
        observed = str(event.payload["terminal_observed_at"])
        not_after = str(event.payload["capture_not_after"])

        now = utc_now_iso()
        aborted = final_repo.abort_running_task(
            connection, task_id, code, now, observed, not_after
        )
        if not aborted:
            connection.rollback()
            raise FinalizationError("TASK_NOT_RUNNING", 409)
        final_repo.insert_rejected_event(
            connection,
            str(event.event_id),
            payload_hash,
            "task_completed",
            now,
            input_row_id,
            task_id,
            code,
            self._legacy_hash(event),
        )
        connection.commit()
        return {
            "event_id": str(event.event_id),
            "status": "rejected",
            "outcome": "rejected",
            "input_id": input_row_id,
            "task_id": task_id,
            "dispatch_authorized": False,
        }

    def _freeze(
        self,
        task_id: str,
        tree_id: str,
        generation: int,
        snapshot: FinalCaptureSnapshot,
        deadline: float,
    ) -> None:
        with connect(self._database_path) as connection:
            now = self._monotonic()
            # Positive remainders round up so a sub-ms remainder
            # still grants a 1ms lock wait instead of no wait.
            remaining_ms = max(0, math.ceil((deadline - now) * 1000))
            connection.execute(f"PRAGMA busy_timeout = {remaining_ms}")
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                if now >= deadline or self._monotonic() >= deadline:
                    raise FinalizationError(
                        "FINAL_SNAPSHOT_TIMEOUT"
                    ) from error
                raise
            if self._monotonic() >= deadline:
                connection.rollback()
                raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
            if not final_repo.publication_is_current(
                connection, task_id, tree_id, generation
            ):
                connection.rollback()
                raise FinalizationError("STALE_CAPTURE_GENERATION", 409)
            if self._monotonic() >= deadline:
                connection.rollback()
                raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
            for row in snapshot.baseline_files:
                final_repo.insert_baseline_file(connection, task_id, row)
            if self._monotonic() >= deadline:
                connection.rollback()
                raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
            for row in snapshot.changes:
                final_repo.insert_file_change(connection, task_id, row)
            if self._monotonic() >= deadline:
                connection.rollback()
                raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
            frozen_at = utc_now_iso()
            if self._monotonic() >= deadline:
                connection.rollback()
                raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
            completeness = _evidence_completeness(snapshot.changes)
            if completeness == "unavailable":
                connection.rollback()
                raise FinalizationError("BASELINE_OBJECT_UNAVAILABLE")
            changed = connection.execute(
                "UPDATE tasks SET final_head = ?, final_branch = ?, "
                "final_status = ?, final_index_manifest = ?, "
                "snapshot_frozen_at = ?, evidence_completeness = ? "
                "WHERE id = ? AND status = 'finalizing' "
                "AND capture_generation = ? AND snapshot_frozen_at IS NULL",
                (
                    snapshot.head,
                    snapshot.branch,
                    snapshot.status,
                    snapshot.index,
                    frozen_at,
                    completeness,
                    task_id,
                    generation,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise FinalizationError("STALE_CAPTURE_GENERATION", 409)
            if self._monotonic() >= deadline:
                connection.rollback()
                raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")
            connection.commit()

    def materialize(self, task_id: str) -> None:
        # Phase 1 reads the frozen SQLite evidence and
        # closes the read connection before any compute,
        # so no writer is held during patch calculation.
        # Phase 2 computes patches purely from those DB
        # blobs (never Git/worktree). Phase 3 publishes
        # in a short atomic transaction that revalidates
        # eligibility plus the full materialization
        # identity (frozen row + baseline/changes blobs).
        with connect(self._database_path) as connection:
            snapshot = final_repo.get_materialization_snapshot(
                connection, task_id
            )
        if snapshot is None:
            return
        if snapshot.status == "completed":
            return
        if (
            snapshot.status != "finalizing"
            or snapshot.snapshot_frozen_at is None
        ):
            return
        baseline = {item.path: item for item in snapshot.baseline}
        computed: dict[str, str | None] = {}
        ordered: list[str] = []
        for change in snapshot.changes:
            if self._patch_hook is not None:
                self._patch_hook()
            # Corrupt gzip raises definitively; partial
            # evidence (hash_only/unsupported) stays None.
            patch = self._patch(baseline.get(change.path), change)
            computed[change.path] = patch
            if patch:
                ordered.append(patch)
        task_diff = "".join(ordered)
        expected = snapshot.identity
        expected_changes = {
            item.path: (
                item.evidence_status,
                item.final_sha256,
                item.final_size,
            )
            for item in snapshot.changes
        }
        now = utc_now_iso()
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                published = final_repo.publish_materialization_locked(
                    connection,
                    task_id,
                    expected,
                    expected_changes,
                    computed,
                    task_diff,
                    now,
                )
            except Exception:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                raise
            if published:
                connection.commit()
            else:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
        if published:
            return
        with connect(self._database_path) as connection:
            state = final_repo.get_finalization_task_status(
                connection, task_id
            )
        if state is None or state == "completed":
            return
        raise FinalizationError("STALE_CAPTURE_GENERATION", 409)

    def recover(self) -> None:
        with connect(self._database_path) as connection:
            entries = final_repo.list_recovery_tasks(connection)
        for entry in entries:
            if entry.snapshot_frozen_at is not None:
                try:
                    self.materialize(entry.task_id)
                except sqlite3.OperationalError as error:
                    if _is_transient_lock(error):
                        continue
                    self._fail(
                        entry.task_id,
                        None,
                        "FINAL_MATERIALIZATION_FAILED",
                    )
                except FinalizationError as error:
                    if error.code in (
                        "STALE_CAPTURE_GENERATION",
                        "FINALIZATION_IN_PROGRESS",
                    ):
                        continue
                    self._fail(
                        entry.task_id,
                        None,
                        error.code,
                    )
                except Exception:
                    self._fail(
                        entry.task_id,
                        None,
                        "FINAL_MATERIALIZATION_FAILED",
                    )
            else:
                self._fail(entry.task_id, None, "FINAL_SNAPSHOT_NOT_FROZEN")

    def fence_unfrozen(self, tree_id: str) -> int:
        # Fence stays under the shared boundary lock so it is atomic
        # against begin->start; the DB fence stays authoritative and
        # worker cancellation is best-effort after commit.
        with self._runner.boundary_lock:
            with connect(self._database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                generation = final_repo.fence_finalization(
                    connection, tree_id, utc_now_iso()
                )
                connection.commit()
        try:
            self._runner.cancel_tree_captures(tree_id)
        except Exception:
            pass
        return generation

    def _validate_event(self, event: EventRequest) -> None:
        if event.event_type != "task_completed":
            raise FinalizationError("UNKNOWN_EVENT_TYPE")
        if event.payload_version != 1:
            raise FinalizationError("UNSUPPORTED_PAYLOAD_VERSION")
        if (
            event.occurred_at.tzinfo is None
            or event.occurred_at.utcoffset() != timedelta(0)
        ):
            raise FinalizationError("OCCURRED_AT_MUST_BE_UTC")
        if (
            not event.git_root.is_absolute()
            or not event.workspace_path.is_absolute()
        ):
            raise FinalizationError("PATH_MUST_BE_ABSOLUTE")
        if event.adapter != SUPPORTED_ADAPTER:
            raise FinalizationError("UNSUPPORTED_COMPATIBILITY_PROFILE")
        if event.adapter_version != SUPPORTED_ADAPTER_VERSION:
            raise FinalizationError("UNSUPPORTED_COMPATIBILITY_PROFILE")
        if not event.execution_id:
            raise FinalizationError("EXECUTION_ID_REQUIRED")
        expected = {
            "task_id",
            "terminal_signal",
            "terminal_outcome",
            "compatibility_profile",
            "terminal_observed_at",
            "capture_not_after",
        }
        if set(event.payload) != expected:
            raise FinalizationError("INVALID_TERMINAL_SIGNAL")
        if event.payload.get("terminal_signal") != SUPPORTED_SIGNAL:
            raise FinalizationError("INVALID_TERMINAL_SIGNAL")
        if event.payload.get("terminal_outcome") != SUPPORTED_OUTCOME:
            raise FinalizationError("INVALID_TERMINAL_OUTCOME")
        if event.payload.get("compatibility_profile") != SUPPORTED_PROFILE:
            raise FinalizationError("UNSUPPORTED_COMPATIBILITY_PROFILE")
        observed = _parse_utc_timestamp(
            event.payload.get("terminal_observed_at"),
            "INVALID_TERMINAL_OBSERVED_AT",
        )
        not_after = _parse_utc_timestamp(
            event.payload.get("capture_not_after"),
            "INVALID_CAPTURE_NOT_AFTER",
        )
        if observed > not_after:
            raise FinalizationError("INVALID_CAPTURE_WINDOW")
        if self._max_window_seconds is None:
            raise FinalizationError("TERMINAL_AUTHORIZATION_UNCONFIGURED", 500)
        if (not_after - observed).total_seconds() > self._max_window_seconds:
            raise FinalizationError("INVALID_CAPTURE_WINDOW")
        if observed > self._clock():
            raise FinalizationError("INVALID_TERMINAL_OBSERVED_AT")

    def _validate_abort_event(self, event: EventRequest) -> None:
        if event.event_type != SUPPORTED_ABORT_EVENT:
            raise FinalizationError("UNKNOWN_EVENT_TYPE")
        if event.payload_version != 1:
            raise FinalizationError("UNSUPPORTED_PAYLOAD_VERSION")
        if (
            event.occurred_at.tzinfo is None
            or event.occurred_at.utcoffset() != timedelta(0)
        ):
            raise FinalizationError("OCCURRED_AT_MUST_BE_UTC")
        if (
            not event.git_root.is_absolute()
            or not event.workspace_path.is_absolute()
        ):
            raise FinalizationError("PATH_MUST_BE_ABSOLUTE")
        if event.adapter != SUPPORTED_ADAPTER:
            raise FinalizationError("UNSUPPORTED_COMPATIBILITY_PROFILE")
        if event.adapter_version != SUPPORTED_ADAPTER_VERSION:
            raise FinalizationError("UNSUPPORTED_COMPATIBILITY_PROFILE")
        if not event.execution_id:
            raise FinalizationError("EXECUTION_ID_REQUIRED")
        if set(event.payload) != {"task_id", "abort_reason"}:
            raise FinalizationError("INVALID_ABORT_REASON")
        task_id = event.payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise FinalizationError("TASK_ID_REQUIRED")
        reason = event.payload.get("abort_reason")
        if reason not in SUPPORTED_ABORT_REASONS:
            raise FinalizationError("INVALID_ABORT_REASON")

    def _validate_correlation(
        self, connection: Any, event: EventRequest, task: FinalizationTask
    ) -> None:
        tree_id = sessions_repo.get_working_tree_id(
            connection, str(event.git_root)
        )
        if (
            task.adapter != event.adapter
            or task.agent_session_id != event.agent_session_id
            or task.tree_id != tree_id
            or task.project_id != str(event.project_id)
            or Path(task.git_root) != Path(event.git_root)
            or Path(task.workspace_path or "") != event.workspace_path
        ):
            raise FinalizationError("TASK_CORRELATION_MISMATCH", 409)
        if (
            not task.execution_id
            or not event.execution_id
            or task.execution_id != event.execution_id
        ):
            raise FinalizationError("EXECUTION_ID_MISMATCH", 409)

    def _task_id(self, event: EventRequest) -> str:
        task_id = event.payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise FinalizationError("TASK_ID_REQUIRED")
        return task_id

    def _abort_reason(self, event: EventRequest) -> str:
        reason = event.payload.get("abort_reason")
        if reason not in SUPPORTED_ABORT_REASONS:
            raise FinalizationError("INVALID_ABORT_REASON")
        return str(reason)

    def _was_fenced(self, task_id: str, tree_id: str, generation: int) -> bool:
        try:
            with connect(self._database_path) as connection:
                return not final_repo.publication_is_current(
                    connection, task_id, tree_id, generation
                )
        except Exception:
            return False

    def _frozen_generation(self, task_id: str) -> int | None:
        # Geração já disponível na linha/snapshot da frozen
        # task; sem nova transição ou inferência.
        try:
            with connect(self._database_path) as connection:
                snapshot = final_repo.get_materialization_snapshot(
                    connection, task_id
                )
        except Exception:
            return None
        if snapshot is None:
            return None
        return snapshot.identity.capture_generation

    def _fail(self, task_id: str, generation: int | None, code: str) -> None:
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = final_repo.fail_task(
                connection, task_id, code, utc_now_iso(), generation
            )
            if changed:
                connection.execute(
                    "UPDATE inbound_events SET status = 'rejected', "
                    "outcome = 'rejected', failure_code = ?, "
                    "failure_message = ? WHERE task_id = ? "
                    "AND event_type = 'task_completed' "
                    "AND status = 'processing'",
                    (code, code, task_id),
                )
            connection.commit()

    def _patch(
        self, baseline: BaselineFileRow | None, change: TaskFileChangeRow
    ) -> str | None:
        if change.evidence_status != "complete":
            # Honest partial evidence carries no patch;
            # completion stays allowed with partial flag.
            return None
        # A missing blob is only legitimate on the absent
        # side (added has no baseline, deleted has no
        # final). Any other missing/corrupt blob with
        # complete evidence fails definitively instead of
        # collapsing to None plus a completed task.
        if baseline is None:
            if change.operation != "added":
                raise FinalizationError("FINAL_MATERIALIZATION_FAILED", 500)
            before_raw = b""
        elif baseline.content is None:
            raise FinalizationError("FINAL_MATERIALIZATION_FAILED", 500)
        else:
            try:
                before_raw = gzip.decompress(baseline.content)
            except (OSError, EOFError) as error:
                raise FinalizationError(
                    "FINAL_MATERIALIZATION_FAILED", 500
                ) from error
        if change.final_content is None:
            if change.operation != "deleted":
                raise FinalizationError("FINAL_MATERIALIZATION_FAILED", 500)
            after_raw = b""
        else:
            try:
                after_raw = gzip.decompress(change.final_content)
            except (OSError, EOFError) as error:
                raise FinalizationError(
                    "FINAL_MATERIALIZATION_FAILED", 500
                ) from error
        # No truncation: SQLite generation is the
        # authority, so the full unified diff publishes.
        # Strict decode: valid gzip holding non-UTF-8
        # bytes is not honest text evidence (binary goes
        # hash_only per capture, never complete). A
        # replacement-char patch would adulterate frozen
        # evidence, so undecodable blobs fail
        # definitively instead of completing as complete.
        try:
            before = before_raw.decode("utf-8")
            after = after_raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise FinalizationError(
                "FINAL_MATERIALIZATION_FAILED", 500
            ) from error
        return "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{change.path}",
                tofile=f"b/{change.path}",
            )
        )

    def _legacy_hash(self, event: EventRequest) -> str:
        return canonical_json_sha256(
            event.model_dump(mode="json", exclude_none=True)
        )

    def _stored_matches(
        self,
        event: EventRequest,
        incoming_transport: str,
        row: FinalizationEventRow,
    ) -> bool:
        if row.payload_hash == incoming_transport:
            return True
        incoming_semantic = self._legacy_hash(event)
        if row.semantic_hash is not None:
            return row.semantic_hash == incoming_semantic
        return row.payload_hash == incoming_semantic

    def _reconcile(
        self,
        event_id: str,
        payload_hash: str,
        row: FinalizationEventRow,
        event: EventRequest,
    ) -> dict[str, object]:
        context = EventHashContext(event=event, payload_hash=payload_hash)
        if not self._stored_matches(context.event, context.payload_hash, row):
            raise FinalizationError("IDEMPOTENCY_CONFLICT", 409)
        if row.status in ("processing", "received"):
            # Frozen-finalizing retry: an idempotent replay of the
            # same task_completed retries only SQLite evidence
            # materialization (no Git recapture, no runner). Locks
            # e conflitos transitórios ficam 409 sem _fail; erros
            # definitivos terminalizam via _fail como no fluxo
            # inicial. Scoped to this task only -- never global.
            if event.event_type == "task_completed" and row.task_id:
                try:
                    self.materialize(row.task_id)
                except FinalizationError as error:
                    if error.code in _RETRYABLE_CONFLICT_CODES:
                        raise
                    # Erro definitivo: espelha o fluxo inicial e
                    # terminaliza a frozen task (sem Git/recover).
                    # _fail é atômico com a resposta: se persistir
                    # falhar com lock, devolve 409 retryable em vez
                    # de mascarar como definitivo.
                    generation = self._frozen_generation(row.task_id)
                    try:
                        self._fail(
                            row.task_id,
                            generation,
                            error.code,
                        )
                    except Exception as fail_error:
                        if _is_transient_lock(fail_error):
                            raise FinalizationError(
                                "FINALIZATION_IN_PROGRESS", 409
                            ) from fail_error
                        raise fail_error from error
                    raise
                except Exception as error:
                    if _is_transient_lock(error):
                        raise FinalizationError(
                            "FINALIZATION_IN_PROGRESS", 409
                        ) from error
                    generation = self._frozen_generation(row.task_id)
                    try:
                        self._fail(
                            row.task_id,
                            generation,
                            "FINALIZATION_FAILED",
                        )
                    except Exception as fail_error:
                        if _is_transient_lock(fail_error):
                            raise FinalizationError(
                                "FINALIZATION_IN_PROGRESS", 409
                            ) from fail_error
                        raise fail_error from error
                    raise FinalizationError(
                        "FINALIZATION_FAILED", 500
                    ) from error
                with connect(self._database_path) as connection:
                    updated = final_repo.get_finalization_event(
                        connection, event_id
                    )
                if updated is not None and updated.status not in (
                    "processing",
                    "received",
                ):
                    return self._reconcile(
                        event_id, payload_hash, updated, event
                    )
            raise FinalizationError("FINALIZATION_IN_PROGRESS", 409)
        if row.status == "rejected":
            if row.failure_code == "CAPTURE_AUTHORIZATION_EXPIRED":
                return {
                    "event_id": event_id,
                    "status": row.status,
                    "outcome": row.outcome,
                    "input_id": row.input_id,
                    "task_id": row.task_id,
                    "dispatch_authorized": False,
                }
            if (
                row.failure_code is not None
                and row.failure_code in SUPPORTED_ABORT_REASONS
            ):
                return {
                    "event_id": event_id,
                    "status": row.status,
                    "outcome": row.outcome,
                    "input_id": row.input_id,
                    "task_id": row.task_id,
                    "dispatch_authorized": False,
                }
            code = row.failure_code or "FINALIZATION_REJECTED"
            raise FinalizationError(code, _replay_status(code))
        return {
            "event_id": event_id,
            "status": row.status,
            "outcome": row.outcome,
            "input_id": row.input_id,
            "task_id": row.task_id,
            "dispatch_authorized": False,
        }
