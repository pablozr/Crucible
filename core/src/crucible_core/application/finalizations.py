from __future__ import annotations

import difflib
import gzip
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from crucible_core.core.database import connect
from crucible_core.core.errors import FinalizationError, ProjectError
from crucible_core.repositories import finalizations_repository as final_repo
from crucible_core.repositories import sessions_repository as sessions_repo
from crucible_core.repositories import tasks_repository as tasks_repo
from crucible_core.schemas.admissions import EventRequest
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


class FinalizationCoordinator:
    def __init__(
        self,
        database_path: Path,
        *,
        capture_final: Callable[..., dict[str, Any]],
        publication_hook: Callable[[], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        max_authorization_window_seconds: int | None = None,
    ) -> None:
        self._database_path = database_path
        self._capture_final = capture_final
        self._publication_hook = publication_hook
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_window_seconds = max_authorization_window_seconds

    def complete(self, event: EventRequest) -> dict[str, object]:
        event_id = str(event.event_id)
        payload_hash = canonical_json_sha256(
            event.model_dump(mode="json", exclude_none=True)
        )
        self._validate_event(event)

        with connect(self._database_path) as connection:
            existing = final_repo.get_finalization_event(connection, event_id)
            if existing is not None:
                return self._reconcile(event_id, payload_hash, existing)

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

        try:
            begun = self._begin(event, task_id, payload_hash, not_after)
        except sqlite3.IntegrityError:
            with connect(self._database_path) as connection:
                existing = final_repo.get_finalization_event(
                    connection, event_id
                )
                if existing is None:
                    raise
                return self._reconcile(event_id, payload_hash, existing)
        if isinstance(begun, dict):
            return begun
        generation, task, input_row_id = begun
        deadline = time.monotonic() + FINAL_CAPTURE_DEADLINE_SECONDS
        try:
            # resolve_project performs Git reads, so it must only run after
            # _begin's transaction has rechecked the capture window and
            # consumed the authorization; a project failure here fails the
            # task instead of letting capture run.
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
            snapshot = self._capture_final(
                Path(task.git_root),
                task.baseline_head,
                task.baseline_branch,
                task.baseline_index_manifest,
                baseline_files,
                project.max_snapshot_file_size_bytes,
                deadline,
            )
            if self._publication_hook is not None:
                self._publication_hook()
            self._freeze(task_id, task.tree_id, generation, snapshot)
            self.materialize(task_id)
        except FinalizationError as error:
            self._fail(task_id, generation, error.code)
            raise
        except Exception:
            self._fail(task_id, generation, "FINALIZATION_FAILED")
            raise FinalizationError("FINALIZATION_FAILED", 500) from None

        return {
            "event_id": event_id,
            "status": "accepted",
            "outcome": "completed",
            "input_id": input_row_id,
            "task_id": task_id,
            "dispatch_authorized": False,
        }

    def abort(self, event: EventRequest) -> dict[str, object]:
        event_id = str(event.event_id)
        payload_hash = canonical_json_sha256(
            event.model_dump(mode="json", exclude_none=True)
        )
        self._validate_abort_event(event)

        with connect(self._database_path) as connection:
            existing = final_repo.get_finalization_event(connection, event_id)
            if existing is not None:
                return self._reconcile(event_id, payload_hash, existing)

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
                    return self._reconcile(event_id, payload_hash, duplicate)
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
                connection.execute(
                    "INSERT INTO inbound_events (id, payload_hash, status, "
                    "event_type, received_at, outcome, input_id, task_id, "
                    "failure_code, failure_message) VALUES (?, ?, "
                    "'rejected', 'task_finalization_aborted', ?, 'rejected', "
                    "?, ?, ?, ?)",
                    (
                        event_id,
                        payload_hash,
                        now,
                        stored.id,
                        task_id,
                        reason,
                        reason,
                    ),
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
                        event_id, payload_hash, existing_retry
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
    ) -> tuple[int, FinalizationTask, str] | dict[str, object]:
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = final_repo.get_finalization_event(
                connection, str(event.event_id)
            )
            if duplicate is not None:
                connection.rollback()
                return self._reconcile(
                    str(event.event_id), payload_hash, duplicate
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
                return self._reject_expired_locked(
                    connection, event, task_id, payload_hash, stored.id
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
            connection.execute(
                "INSERT INTO inbound_events (id, payload_hash, status, "
                "event_type, received_at, outcome, input_id, task_id) "
                "VALUES (?, ?, 'processing', 'task_completed', ?, "
                "'finalizing', ?, ?)",
                (
                    str(event.event_id),
                    payload_hash,
                    utc_now_iso(),
                    stored.id,
                    task_id,
                ),
            )
            connection.commit()
            return generation, task, stored.id

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
                    str(event.event_id), payload_hash, duplicate
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
        connection.execute(
            "INSERT INTO inbound_events (id, payload_hash, status, "
            "event_type, received_at, outcome, input_id, task_id, "
            "failure_code, failure_message) VALUES (?, ?, "
            "'rejected', 'task_completed', ?, 'rejected', ?, ?, "
            "?, ?)",
            (
                str(event.event_id),
                payload_hash,
                now,
                input_row_id,
                task_id,
                code,
                code,
            ),
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
        snapshot: dict[str, Any],
    ) -> None:
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not final_repo.publication_is_current(
                connection, task_id, tree_id, generation
            ):
                raise FinalizationError("STALE_CAPTURE_GENERATION", 409)
            for row in snapshot["baseline_files"]:
                final_repo.insert_baseline_file(connection, task_id, row)
            for row in snapshot["changes"]:
                final_repo.insert_file_change(connection, task_id, row)
            frozen_at = utc_now_iso()
            changed = connection.execute(
                "UPDATE tasks SET final_head = ?, final_branch = ?, "
                "final_status = ?, final_index_manifest = ?, "
                "snapshot_frozen_at = ?, evidence_completeness = ? "
                "WHERE id = ? AND status = 'finalizing' "
                "AND capture_generation = ? AND snapshot_frozen_at IS NULL",
                (
                    snapshot["head"],
                    snapshot["branch"],
                    snapshot["status"],
                    snapshot["index"],
                    frozen_at,
                    "complete"
                    if all(
                        row.evidence_status == "complete"
                        for row in snapshot["changes"]
                    )
                    else "partial",
                    task_id,
                    generation,
                ),
            ).rowcount
            if changed != 1:
                raise FinalizationError("STALE_CAPTURE_GENERATION", 409)
            connection.commit()

    def materialize(self, task_id: str) -> None:
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = final_repo.get_frozen_task_state(connection, task_id)
            if (
                state is None
                or state.status != "finalizing"
                or state.snapshot_frozen_at is None
            ):
                connection.rollback()
                return
            baseline = {
                item.path: item
                for item in tasks_repo.list_task_baseline_files(
                    connection, task_id
                )
            }
            changes = tasks_repo.list_task_file_changes(connection, task_id)
            patches: list[str] = []
            for change in changes:
                patch = self._patch(baseline.get(change.path), change)
                connection.execute(
                    "UPDATE task_file_changes SET patch = ?, "
                    "materialized_at = ? WHERE task_id = ? AND path = ?",
                    (patch, utc_now_iso(), task_id, change.path),
                )
                if patch:
                    patches.append(patch)
            connection.execute(
                "UPDATE tasks SET status = 'completed', task_diff = ?, "
                "completed_at = ? WHERE id = ? AND status = 'finalizing' "
                "AND snapshot_frozen_at IS NOT NULL",
                ("".join(patches), utc_now_iso(), task_id),
            )
            connection.execute(
                "UPDATE inbound_events SET status = 'accepted', "
                "outcome = 'completed' WHERE task_id = ? "
                "AND event_type = 'task_completed' AND status = 'processing'",
                (task_id,),
            )
            connection.commit()

    def recover(self) -> None:
        with connect(self._database_path) as connection:
            entries = final_repo.list_recovery_tasks(connection)
        for entry in entries:
            if entry.snapshot_frozen_at is not None:
                try:
                    self.materialize(entry.task_id)
                except Exception:
                    self._fail(
                        entry.task_id,
                        None,
                        "FINAL_MATERIALIZATION_FAILED",
                    )
            else:
                self._fail(entry.task_id, None, "FINAL_SNAPSHOT_NOT_FROZEN")

    def fence_unfrozen(self, tree_id: str) -> int:
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            generation = final_repo.fence_finalization(
                connection, tree_id, utc_now_iso()
            )
            connection.commit()
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
            return None
        before = (
            gzip.decompress(baseline.content).decode("utf-8", "replace")
            if baseline is not None and baseline.content is not None
            else ""
        )
        after = (
            gzip.decompress(change.final_content).decode("utf-8", "replace")
            if change.final_content is not None
            else ""
        )
        return "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{change.path}",
                tofile=f"b/{change.path}",
            )
        )

    def _reconcile(
        self,
        event_id: str,
        payload_hash: str,
        row: FinalizationEventRow,
    ) -> dict[str, object]:
        if row.payload_hash != payload_hash:
            raise FinalizationError("IDEMPOTENCY_CONFLICT", 409)
        if row.status in ("processing", "received"):
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
