from __future__ import annotations

import difflib
import gzip
import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from crucible_core.core.database import connect
from crucible_core.core.errors import FinalizationError, ProjectError
from crucible_core.repositories import finalizations_repository as final_repo
from crucible_core.repositories import sessions_repository as sessions_repo
from crucible_core.repositories import tasks_repository as tasks_repo
from crucible_core.schemas.admissions import EventRequest
from crucible_core.schemas.persistence import TaskFileChangeRow
from crucible_core.services.projects import resolve_project
from crucible_core.utils.functions import canonical_json_sha256, utc_now_iso

FINAL_CAPTURE_DEADLINE_SECONDS = 5
SUPPORTED_ADAPTER = "opencode-v1"
SUPPORTED_ADAPTER_VERSION = "0.1.0"
SUPPORTED_PROFILE = "opencode-v1-1.18.28-write-stop-restricted"
SUPPORTED_SIGNAL = "session_prompt_return"
SUPPORTED_OUTCOME = "stop"

_RETRYABLE_CONFLICT_CODES = frozenset(
    {
        "IDEMPOTENCY_CONFLICT",
        "INPUT_TASK_MISMATCH",
        "STALE_CAPTURE_GENERATION",
        "TASK_CORRELATION_MISMATCH",
        "TASK_NOT_RUNNING",
        "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT",
        "FINALIZATION_IN_PROGRESS",
    }
)

_SERVER_FAILURE_CODES = frozenset(
    {
        "FINALIZATION_FAILED",
        "FINAL_MATERIALIZATION_FAILED",
        "FINAL_SNAPSHOT_NOT_FROZEN",
    }
)


def _replay_status(code: str) -> int:
    if code == "TASK_NOT_FOUND":
        return 404
    if code in _RETRYABLE_CONFLICT_CODES:
        return 409
    if code in _SERVER_FAILURE_CODES:
        return 500
    return 400


class FinalizationCoordinator:
    def __init__(
        self,
        database_path: Path,
        *,
        capture_final: Callable[..., dict[str, Any]],
        publication_hook: Callable[[], None] | None = None,
    ) -> None:
        self._database_path = database_path
        self._capture_final = capture_final
        self._publication_hook = publication_hook

    def complete(self, event: EventRequest) -> dict[str, object]:
        event_id = str(event.event_id)
        payload_hash = canonical_json_sha256(
            event.model_dump(mode="json", exclude_none=True)
        )
        self._validate_event(event)

        with connect(self._database_path) as connection:
            existing = connection.execute(
                "SELECT payload_hash, status, outcome, input_id, task_id, "
                "failure_code FROM inbound_events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                return self._reconcile(event_id, payload_hash, existing)

        task_id = self._task_id(event)
        try:
            project = resolve_project(event.git_root)
        except ProjectError as error:
            raise FinalizationError(str(error)) from error
        if project.id != str(event.project_id):
            raise FinalizationError("PROJECT_ID_MISMATCH")

        try:
            begun = self._begin(event, task_id, payload_hash)
        except sqlite3.IntegrityError:
            with connect(self._database_path) as connection:
                existing = connection.execute(
                    "SELECT payload_hash, status, outcome, input_id, "
                    "task_id, failure_code FROM inbound_events "
                    "WHERE id = ?",
                    (event_id,),
                ).fetchone()
                if existing is None:
                    raise
                return self._reconcile(event_id, payload_hash, existing)
        if isinstance(begun, dict):
            return begun
        generation, task, input_row_id = begun
        deadline = time.monotonic() + FINAL_CAPTURE_DEADLINE_SECONDS
        try:
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

    def _begin(
        self,
        event: EventRequest,
        task_id: str,
        payload_hash: str,
    ) -> tuple[int, Any, str] | dict[str, object]:
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT payload_hash, status, outcome, input_id, "
                "task_id, failure_code FROM inbound_events "
                "WHERE id = ?",
                (str(event.event_id),),
            ).fetchone()
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

            generation = final_repo.begin_finalization(
                connection,
                task_id,
                task.tree_id,
                event.execution_id or "",
                event.payload["terminal_signal"],
                event.payload["terminal_outcome"],
                event.payload["compatibility_profile"],
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
            row = connection.execute(
                "SELECT status, snapshot_frozen_at FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None or row[0] != "finalizing" or row[1] is None:
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
            rows = final_repo.list_recovery_tasks(connection)
        for task_id, frozen_at in rows:
            if frozen_at is not None:
                try:
                    self.materialize(task_id)
                except Exception:
                    self._fail(task_id, None, "FINAL_MATERIALIZATION_FAILED")
            else:
                self._fail(task_id, None, "FINAL_SNAPSHOT_NOT_FROZEN")

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
        }
        if set(event.payload) != expected:
            raise FinalizationError("INVALID_TERMINAL_SIGNAL")
        if event.payload.get("terminal_signal") != SUPPORTED_SIGNAL:
            raise FinalizationError("INVALID_TERMINAL_SIGNAL")
        if event.payload.get("terminal_outcome") != SUPPORTED_OUTCOME:
            raise FinalizationError("INVALID_TERMINAL_OUTCOME")
        if event.payload.get("compatibility_profile") != SUPPORTED_PROFILE:
            raise FinalizationError("UNSUPPORTED_COMPATIBILITY_PROFILE")

    def _validate_correlation(
        self, connection: Any, event: EventRequest, task: Any
    ) -> None:
        tree_id = sessions_repo.get_working_tree_id(
            connection, str(Path(event.git_root).resolve())
        )
        if (
            task.adapter != event.adapter
            or task.agent_session_id != event.agent_session_id
            or task.tree_id != tree_id
            or task.project_id != str(event.project_id)
            or Path(task.git_root) != Path(event.git_root).resolve()
            or Path(task.workspace_path or "") != event.workspace_path
        ):
            raise FinalizationError("TASK_CORRELATION_MISMATCH", 409)

    def _task_id(self, event: EventRequest) -> str:
        task_id = event.payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise FinalizationError("TASK_ID_REQUIRED")
        return task_id

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

    def _patch(self, baseline: Any, change: TaskFileChangeRow) -> str | None:
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
        row: tuple[Any, ...],
    ) -> dict[str, object]:
        if row[0] != payload_hash:
            raise FinalizationError("IDEMPOTENCY_CONFLICT", 409)
        if row[1] in ("processing", "received"):
            raise FinalizationError("FINALIZATION_IN_PROGRESS", 409)
        if row[1] == "rejected":
            code = row[5] or "FINALIZATION_REJECTED"
            raise FinalizationError(code, _replay_status(code))
        return {
            "event_id": event_id,
            "status": row[1],
            "outcome": row[2],
            "input_id": row[3],
            "task_id": row[4],
            "dispatch_authorized": False,
        }
