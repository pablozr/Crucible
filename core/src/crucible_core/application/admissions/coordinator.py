from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from crucible_core.core.database import connect
from crucible_core.core.errors import AdmissionError, ProjectError
from crucible_core.infrastructure.git import final_capture_worker as worker
from crucible_core.logging import get_logger
from crucible_core.repositories import admissions_repository as admissions_repo
from crucible_core.repositories import finalizations_repository as final_repo
from crucible_core.repositories import sessions_repository as sessions_repo
from crucible_core.repositories import tasks_repository as tasks_repo
from crucible_core.responses.admissions import event_response
from crucible_core.schemas.admissions import EventRequest
from crucible_core.schemas.git import BaselineCaptureSnapshot
from crucible_core.schemas.persistence import (
    InboundEvent,
    NewAcceptedEvent,
    NewNoInputDecision,
    NewStoredInput,
    StoredInput,
)
from crucible_core.schemas.projects import Project
from crucible_core.services.projects import resolve_project
from crucible_core.utils.functions import utc_now_iso

from . import candidates as candidate_support
from . import decisions as decisions
from . import replay as replay_support

CAPTURE_DEADLINE_SECONDS = 2

logger = get_logger(__name__)


_MAX_ROUTE_ATTEMPTS = 3


class AdmissionCoordinator:
    def __init__(
        self,
        database_path: Path,
        *,
        capture_baseline: Callable[
            [Path, int, float], BaselineCaptureSnapshot
        ],
        race_hook: Any | None = None,
        capture_runner: worker.CaptureRunner | None = None,
    ) -> None:
        self._database_path = database_path
        self._capture_baseline = capture_baseline
        self._race_hook = race_hook
        self._runner = capture_runner or worker.get_default_runner()

    def _check_session_tree_mismatch(
        self,
        connection: sqlite3.Connection,
        event: EventRequest,
        tree_id: str | None,
    ) -> None:
        found = sessions_repo.find_session_tree(
            connection, event.adapter, event.agent_session_id
        )
        if found is None:
            return
        if decisions.is_session_tree_mismatch(found.tree_id, tree_id):
            raise AdmissionError("SESSION_WORKTREE_MISMATCH", 409)

    def _require_execution_match(
        self, stored_execution_id: str | None, event: EventRequest
    ) -> None:
        decisions.check_execution_match(
            stored_execution_id, event.execution_id
        )

    def _fence_unfrozen_finalization_locked(
        self,
        connection: sqlite3.Connection,
        tree_id: str | None,
        now: str,
    ) -> None:
        if tree_id is None:
            return
        unfrozen = connection.execute(
            "SELECT 1 FROM tasks WHERE working_tree_id = ? "
            "AND status = 'finalizing' "
            "AND snapshot_frozen_at IS NULL LIMIT 1",
            (tree_id,),
        ).fetchone()
        if unfrozen is None:
            return
        final_repo.fence_finalization(connection, tree_id, now)

    def _cancel_tree_workers_best_effort(
        self,
        keys: list[tuple[str, int]],
    ) -> None:
        # DB fence stays authoritative; cancellation only reaps the
        # child. Failures are swallowed so the HTTP response still
        # reflects the durable fence/decision.
        for key in keys:
            try:
                self._runner.cancel_capture(key)
            except Exception:
                continue

    def admit(
        self, event: EventRequest, payload_hash: str | None = None
    ) -> dict[str, object]:
        event_id = str(event.event_id)
        if payload_hash is None:
            payload_hash = self._payload_hash(event)
        now = utc_now_iso()
        deadline = time.monotonic() + CAPTURE_DEADLINE_SECONDS

        self._validate_event(event)

        with connect(self._database_path) as connection:
            existing_row = admissions_repo.find_event(connection, event_id)
            if existing_row:
                return self._reconcile_existing(
                    event_id, payload_hash, existing_row, event
                )

        try:
            project = self._resolve_event_project(event)

            replayed = self._replay_semantic_transactional(
                event, event_id, payload_hash, project.git_root
            )
            if replayed is not None:
                return replayed

            with connect(self._database_path) as connection:
                tree_id = sessions_repo.get_working_tree_id(
                    connection, project.git_root
                )
                self._check_session_tree_mismatch(connection, event, tree_id)
                session_row = sessions_repo.find_session_tree(
                    connection, event.adapter, event.agent_session_id
                )
                session_id = session_row.session_id if session_row else None
                joinable = (
                    tasks_repo.find_running_task_by_session(
                        connection, session_id
                    )
                    if session_id is not None
                    else None
                )
                blocking = (
                    tasks_repo.find_active_task_by_tree(connection, tree_id)
                    if tree_id is not None
                    else None
                )

            delivery = event.payload.get("delivery")
            route = decisions.decide_route(
                decisions.RoutingObservation(
                    delivery=delivery,
                    has_joinable=joinable is not None,
                    has_blocking=blocking is not None,
                )
            )

            if route == "steer_without_task":
                steered = self._persist_steer_without_task(
                    event,
                    event_id,
                    payload_hash,
                    now,
                    project.git_root,
                )
                if steered is not None:
                    return steered
                raise AdmissionError("STEER_WITHOUT_ACTIVE_TASK", 409)
            if route == "join":
                assert joinable is not None
                return self._join_active_task(
                    event,
                    event_id,
                    payload_hash,
                    joinable,
                    now,
                    project.git_root,
                    deadline,
                    0,
                )

            if route == "overlap":
                assert blocking is not None
                return self._persist_released_overlap(
                    event,
                    event_id,
                    payload_hash,
                    blocking.id,
                    now,
                    project.git_root,
                    deadline,
                    0,
                )

            baseline = self._capture_baseline(
                Path(project.git_root),
                project.max_snapshot_file_size_bytes,
                deadline,
            )
            candidate_id = str(uuid.uuid4())

            return self._insert_candidate_and_promote(
                event,
                event_id,
                payload_hash,
                candidate_id,
                baseline,
                now,
                deadline,
                project.git_root,
            )
        except AdmissionError as error:
            if error.code in (
                "IDEMPOTENCY_CONFLICT",
                "STEER_WITHOUT_ACTIVE_TASK",
                "SESSION_WORKTREE_MISMATCH",
                "EXECUTION_ID_MISMATCH",
            ):
                raise
            logger.warning(
                "admission rejected code=%s event_id=%s",
                error.code,
                event_id,
            )
            return self._reject_event(
                event, event_id, payload_hash, error.code
            )
        except sqlite3.IntegrityError:
            logger.warning(
                "admission conflict code=ADMISSION_CONFLICT event_id=%s",
                event_id,
            )
            return self._reject_event(
                event,
                event_id,
                payload_hash,
                "ADMISSION_CONFLICT",
            )

    def _insert_candidate_and_promote(
        self,
        event: EventRequest,
        event_id: str,
        payload_hash: str,
        candidate_id: str,
        baseline: BaselineCaptureSnapshot,
        now: str,
        deadline: float,
        git_root: str,
        depth: int = 0,
    ) -> dict[str, object]:
        if depth >= _MAX_ROUTE_ATTEMPTS:
            raise AdmissionError("ADMISSION_CONFLICT", 409)
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = admissions_repo.find_event(connection, event_id)
            if existing:
                connection.rollback()
                return self._reconcile_existing(
                    event_id, payload_hash, existing, event
                )
            replayed = self._replay_semantic_locked(
                connection, event, event_id, payload_hash
            )
            if replayed is not None:
                connection.rollback()
                return replayed
            session_id, tree_id = self._persist_session_locked(
                connection, event, git_root
            )
            joinable = tasks_repo.find_running_task_by_session(
                connection, session_id
            )
            if joinable is not None:
                connection.rollback()
                return self._join_active_task(
                    event,
                    event_id,
                    payload_hash,
                    joinable,
                    now,
                    git_root,
                    deadline,
                    depth + 1,
                )
            blocking = tasks_repo.find_active_task_by_tree(connection, tree_id)
            if blocking is not None:
                connection.rollback()
                return self._persist_released_overlap(
                    event,
                    event_id,
                    payload_hash,
                    blocking.id,
                    now,
                    git_root,
                    deadline,
                    depth + 1,
                )
            stored_input = tasks_repo.find_input(
                connection, session_id, event.input_id
            )
            if stored_input:
                connection.rollback()
                if stored_input.admission_hash != self._admission_hash(event):
                    raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
                stored_owner = tasks_repo.get_task_owner(
                    connection, stored_input.task_id
                )
                self._require_execution_match(
                    stored_owner.execution_id if stored_owner else None,
                    event,
                )
                return self._replay_admitted_input(
                    event, event_id, payload_hash, stored_input
                )
            candidate_support.insert_processing_candidate_locked(
                connection,
                event_id=event_id,
                payload_hash=payload_hash,
                semantic_hash=self._payload_hash(event),
                event_type=event.event_type,
                native_input_id=event.input_id,
                candidate_id=candidate_id,
                session_id=session_id,
                baseline=baseline,
                created_at=now,
                admission_hash=self._admission_hash(event),
            )
            connection.commit()
        return self._promote_candidate(
            event,
            event_id,
            payload_hash,
            candidate_id,
            now,
            deadline,
            git_root,
            depth,
        )

    def _promote_candidate(
        self,
        event: EventRequest,
        event_id: str,
        payload_hash: str,
        candidate_id: str,
        now: str,
        deadline: float,
        git_root: str,
        depth: int = 0,
    ) -> dict[str, object]:
        if depth >= _MAX_ROUTE_ATTEMPTS:
            return self._expire_candidate(
                event_id, payload_hash, event, "ADMISSION_CONFLICT"
            )
        try:
            with connect(self._database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = admissions_repo.find_event(connection, event_id)
                if existing and existing.status != "processing":
                    connection.rollback()
                    return self._reconcile_existing(
                        event_id, payload_hash, existing, event
                    )
                session_id, tree_id = self._persist_session_locked(
                    connection, event, git_root
                )
                blocking = tasks_repo.find_active_task_by_tree(
                    connection, tree_id
                )
                if blocking is not None:
                    connection.rollback()
                    self._expire_candidate_silent(candidate_id)
                    joinable = tasks_repo.find_running_task_by_session(
                        connection, session_id
                    )
                    if joinable is not None:
                        owner = tasks_repo.get_task_owner(connection, joinable)
                        if decisions.is_owned_running_task(
                            decisions.TaskOwnership(
                                session_id=owner.session_id,
                                tree_id=owner.tree_id,
                                status=owner.status,
                            )
                            if owner is not None
                            else None,
                            session_id,
                            tree_id,
                        ):
                            return self._join_active_task(
                                event,
                                event_id,
                                payload_hash,
                                joinable,
                                now,
                                git_root,
                                deadline,
                                depth + 1,
                            )
                    return self._persist_released_overlap(
                        event,
                        event_id,
                        payload_hash,
                        blocking.id,
                        now,
                        git_root,
                        deadline,
                        depth + 1,
                    )
                input_id = str(uuid.uuid4())
                task_id = str(uuid.uuid4())
                candidate = admissions_repo.find_captured_candidate(
                    connection, candidate_id
                )
                if not candidate:
                    existing = admissions_repo.find_event(connection, event_id)
                    connection.rollback()
                    return self._reconcile_existing(
                        event_id, payload_hash, existing, event
                    )

                if time.monotonic() > deadline:
                    connection.rollback()
                    return self._expire_candidate(
                        event_id, payload_hash, event
                    )
                if self._race_hook is not None:
                    hook = self._race_hook
                    connection.commit()
                    try:
                        hook(self._database_path, event, candidate_id)
                    finally:
                        connection.execute("BEGIN IMMEDIATE")
                    candidate = admissions_repo.find_captured_candidate(
                        connection, candidate_id
                    )
                    if not candidate:
                        existing = admissions_repo.find_event(
                            connection, event_id
                        )
                        connection.rollback()
                        return self._reconcile_existing(
                            event_id, payload_hash, existing, event
                        )
                    rerouted = self._reroute_after_race_locked(
                        connection, event, session_id, tree_id
                    )
                    if rerouted is not None:
                        kind, ref_id = rerouted
                        connection.rollback()
                        self._expire_candidate_silent(candidate_id)
                        if kind == "join":
                            return self._join_active_task(
                                event,
                                event_id,
                                payload_hash,
                                ref_id,
                                now,
                                git_root,
                                deadline,
                                depth + 1,
                            )
                        return self._persist_released_overlap(
                            event,
                            event_id,
                            payload_hash,
                            ref_id,
                            now,
                            git_root,
                            deadline,
                            depth + 1,
                        )
                candidate_support.promote_candidate_locked(
                    connection,
                    candidate=candidate,
                    candidate_id=candidate_id,
                    event_id=event_id,
                    task_id=task_id,
                    input_id=input_id,
                    session_id=session_id,
                    tree_id=tree_id,
                    execution_id=event.execution_id or "",
                    created_at=now,
                    admission_hash=self._admission_hash(event),
                    native_input_id=event.input_id,
                )
                connection.commit()
            return event_response(
                event_id, "accepted", "admitted", input_id, task_id
            ).model_dump()
        except sqlite3.IntegrityError:
            logger.warning(
                "admission race detected event_id=%s",
                event_id,
            )
            with connect(self._database_path) as connection:
                joinable = tasks_repo.find_running_task_by_session(
                    connection,
                    self._session_id_or_empty(connection, event),
                )
                tree_id = sessions_repo.get_working_tree_id(
                    connection, git_root
                )
                blocking = (
                    tasks_repo.find_active_task_by_tree(connection, tree_id)
                    if tree_id is not None
                    else None
                )
            self._expire_candidate_silent(candidate_id)
            if joinable is not None:
                return self._join_active_task(
                    event,
                    event_id,
                    payload_hash,
                    joinable,
                    now,
                    git_root,
                    deadline,
                    depth + 1,
                )
            if blocking is not None:
                return self._persist_released_overlap(
                    event,
                    event_id,
                    payload_hash,
                    blocking.id,
                    now,
                    git_root,
                    deadline,
                    depth + 1,
                )
            logger.warning(
                "admission conflict code=ADMISSION_CONFLICT event_id=%s",
                event_id,
            )
            return self._expire_candidate(
                event_id,
                payload_hash,
                event,
                "ADMISSION_CONFLICT",
            )

    def _reject_event(
        self,
        event: EventRequest,
        event_id: str,
        payload_hash: str,
        code: str,
    ) -> dict[str, object]:
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = admissions_repo.find_event(connection, event_id)
            if row:
                if not self._stored_matches(event, payload_hash, row):
                    connection.rollback()
                    raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
                if row.status != "processing":
                    connection.rollback()
                    return self._reconcile_existing(
                        event_id, payload_hash, row, event
                    )
                admissions_repo.mark_event_rejected(connection, event_id, code)
            else:
                admissions_repo.insert_rejected_event(
                    connection,
                    event_id,
                    payload_hash,
                    event.event_type,
                    utc_now_iso(),
                    code,
                    self._payload_hash(event),
                )
            connection.commit()
        return event_response(
            event_id, "rejected", "rejected", None, None
        ).model_dump()

    def _expire_candidate(
        self,
        event_id: str,
        payload_hash: str,
        event: EventRequest,
        code: str = "CANDIDATE_EXPIRED",
    ) -> dict[str, object]:
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = admissions_repo.find_event(connection, event_id)

            if not row or not self._stored_matches(event, payload_hash, row):
                connection.rollback()
                raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)

            candidate_support.expire_event_candidates_locked(
                connection, event_id, code
            )
            connection.commit()

        return event_response(
            event_id, "rejected", "rejected", None, None
        ).model_dump()

    def _validate_event(self, event: EventRequest) -> None:
        if event.event_type != "input_candidate":
            raise AdmissionError("UNKNOWN_EVENT_TYPE")
        if event.payload_version != 1:
            raise AdmissionError("UNSUPPORTED_PAYLOAD_VERSION")
        if not event.execution_id:
            raise AdmissionError("EXECUTION_ID_REQUIRED")
        if event.payload.get("delivery") not in ("new", "steer"):
            raise AdmissionError("UNSUPPORTED_DELIVERY")
        if (
            event.occurred_at.tzinfo is None
            or event.occurred_at.utcoffset() != timedelta(0)
        ):
            raise AdmissionError("OCCURRED_AT_MUST_BE_UTC")
        if (
            not event.git_root.is_absolute()
            or not event.workspace_path.is_absolute()
        ):
            raise AdmissionError("PATH_MUST_BE_ABSOLUTE")

    def _resolve_event_project(self, event: EventRequest) -> Project:
        try:
            project = resolve_project(event.git_root)
        except ProjectError as error:
            logger.warning("project validation failed code=%s", error)
            raise AdmissionError(str(error)) from error
        if str(event.project_id) != project.id:
            raise AdmissionError("PROJECT_ID_MISMATCH")
        return project

    def _persist_session_locked(
        self,
        connection: sqlite3.Connection,
        event: EventRequest,
        git_root: str,
    ) -> tuple[str, str]:
        from crucible_core.services.projects import resolve_project as _resolve

        project_id = str(event.project_id)
        try:
            resolved = _resolve(event.git_root)
            project_id = resolved.id
        except Exception:
            pass
        tree_id = sessions_repo.get_working_tree_id(connection, git_root)
        found = sessions_repo.find_session_tree(
            connection, event.adapter, event.agent_session_id
        )
        if found is not None:
            if decisions.is_session_tree_mismatch(found.tree_id, tree_id):
                raise AdmissionError("SESSION_WORKTREE_MISMATCH", 409)
            return found.session_id, found.tree_id
        if tree_id is None:
            tree_id = str(uuid.uuid4())
            sessions_repo.upsert_project(connection, project_id, git_root)
            sessions_repo.insert_working_tree(
                connection, tree_id, project_id, git_root
            )
        session_id = str(uuid.uuid4())
        sessions_repo.insert_session(
            connection,
            session_id,
            tree_id,
            event.adapter,
            event.agent_session_id,
            event.adapter_version,
            str(event.workspace_path),
        )
        return session_id, tree_id

    def _join_active_task(
        self,
        event: EventRequest,
        event_id: str,
        payload_hash: str,
        hint_task_id: str | None,
        now: str,
        git_root: str,
        deadline: float | None = None,
        depth: int = 0,
    ) -> dict[str, object]:
        if depth >= _MAX_ROUTE_ATTEMPTS:
            raise AdmissionError("ADMISSION_CONFLICT", 409)
        try:
            with connect(self._database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = admissions_repo.find_event(connection, event_id)
                takeover_processing = False
                if existing:
                    if not self._stored_matches(event, payload_hash, existing):
                        connection.rollback()
                        raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
                    if existing.status != "processing":
                        connection.rollback()
                        return self._reconcile_existing(
                            event_id, payload_hash, existing, event
                        )
                    takeover_processing = True
                tree_id = sessions_repo.get_working_tree_id(
                    connection, git_root
                )
                self._check_session_tree_mismatch(connection, event, tree_id)
                session_row = sessions_repo.find_session_tree(
                    connection, event.adapter, event.agent_session_id
                )
                if session_row is None or tree_id is None:
                    connection.rollback()
                    return self._route_without_joinable(
                        event,
                        event_id,
                        payload_hash,
                        now,
                        git_root,
                        deadline,
                        depth + 1,
                    )
                session_id = session_row.session_id
                stored_input = tasks_repo.find_input(
                    connection, session_id, event.input_id
                )
                if stored_input:
                    if stored_input.admission_hash != self._admission_hash(
                        event
                    ):
                        raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
                    stored_owner = tasks_repo.get_task_owner(
                        connection, stored_input.task_id
                    )
                    self._require_execution_match(
                        stored_owner.execution_id if stored_owner else None,
                        event,
                    )
                    if takeover_processing:
                        admissions_repo.mark_event_accepted(
                            connection,
                            event_id,
                            stored_input.id,
                            stored_input.task_id,
                        )
                        connection.commit()
                    else:
                        admissions_repo.insert_accepted_event(
                            connection,
                            NewAcceptedEvent(
                                event_id=event_id,
                                payload_hash=payload_hash,
                                semantic_hash=self._payload_hash(event),
                                event_type=event.event_type,
                                received_at=now,
                                input_id=stored_input.id,
                                task_id=stored_input.task_id,
                            ),
                        )
                        connection.commit()
                    return event_response(
                        event_id,
                        "accepted",
                        "admitted",
                        stored_input.id,
                        stored_input.task_id,
                    ).model_dump()
                decision = admissions_repo.find_no_input_decision(
                    connection,
                    event.adapter,
                    event.agent_session_id,
                    event.input_id,
                )
                if decision:
                    connection.rollback()
                    return self._semantic_join_conflict(
                        event, event_id, payload_hash
                    )
                fresh = tasks_repo.find_running_task_by_session(
                    connection, session_id
                )
                if fresh is None:
                    connection.rollback()
                    return self._route_without_joinable(
                        event,
                        event_id,
                        payload_hash,
                        now,
                        git_root,
                        deadline,
                        depth + 1,
                    )
                owner = tasks_repo.get_task_owner(connection, fresh)
                if not decisions.is_owned_running_task(
                    decisions.TaskOwnership(
                        session_id=owner.session_id,
                        tree_id=owner.tree_id,
                        status=owner.status,
                    )
                    if owner is not None
                    else None,
                    session_id,
                    tree_id,
                ):
                    connection.rollback()
                    return self._route_without_joinable(
                        event,
                        event_id,
                        payload_hash,
                        now,
                        git_root,
                        deadline,
                        depth + 1,
                    )
                self._require_execution_match(owner.execution_id, event)
                input_id = str(uuid.uuid4())
                tasks_repo.insert_input(
                    connection,
                    NewStoredInput(
                        row_id=input_id,
                        session_id=session_id,
                        task_id=fresh,
                        input_id=event.input_id,
                        admission_hash=self._admission_hash(event),
                    ),
                )
                if takeover_processing:
                    admissions_repo.mark_event_accepted(
                        connection, event_id, input_id, fresh
                    )
                else:
                    admissions_repo.insert_accepted_event(
                        connection,
                        NewAcceptedEvent(
                            event_id=event_id,
                            payload_hash=payload_hash,
                            semantic_hash=self._payload_hash(event),
                            event_type=event.event_type,
                            received_at=now,
                            input_id=input_id,
                            task_id=fresh,
                        ),
                    )
                connection.commit()
            return event_response(
                event_id, "accepted", "admitted", input_id, fresh
            ).model_dump()
        except AdmissionError:
            raise
        except sqlite3.IntegrityError as error:
            try:
                with connect(self._database_path) as connection:
                    existing = admissions_repo.find_event(connection, event_id)
                    if existing:
                        return self._reconcile_existing(
                            event_id, payload_hash, existing, event
                        )
            except AdmissionError:
                raise
            logger.warning(
                "admission conflict code=ADMISSION_CONFLICT event_id=%s",
                event_id,
            )
            raise AdmissionError("ADMISSION_CONFLICT", 409) from error

    def _route_without_joinable(
        self,
        event: EventRequest,
        event_id: str,
        payload_hash: str,
        now: str,
        git_root: str,
        deadline: float | None = None,
        depth: int = 0,
    ) -> dict[str, object]:
        if depth >= _MAX_ROUTE_ATTEMPTS:
            raise AdmissionError("ADMISSION_CONFLICT", 409)
        if deadline is None:
            deadline = time.monotonic() + CAPTURE_DEADLINE_SECONDS
        for _attempt in range(depth, _MAX_ROUTE_ATTEMPTS):
            with connect(self._database_path) as connection:
                tree_id = sessions_repo.get_working_tree_id(
                    connection, git_root
                )
                blocking = (
                    tasks_repo.find_active_task_by_tree(connection, tree_id)
                    if tree_id is not None
                    else None
                )
            if event.payload.get("delivery") == "steer":
                steered = self._persist_steer_without_task(
                    event,
                    event_id,
                    payload_hash,
                    now,
                    git_root,
                    deadline,
                    _attempt + 1,
                )
                if steered is not None:
                    return steered
                raise AdmissionError("STEER_WITHOUT_ACTIVE_TASK", 409)
            if blocking is not None:
                return self._persist_released_overlap(
                    event,
                    event_id,
                    payload_hash,
                    blocking.id,
                    now,
                    git_root,
                    deadline,
                    _attempt + 1,
                )
            self._persist_steer_without_task_if_steer(event, event_id)
            project = self._resolve_event_project(event)
            baseline = self._capture_baseline(
                Path(project.git_root),
                project.max_snapshot_file_size_bytes,
                deadline,
            )
            return self._insert_candidate_and_promote(
                event,
                event_id,
                payload_hash,
                str(uuid.uuid4()),
                baseline,
                now,
                deadline,
                git_root,
                _attempt + 1,
            )
        raise AdmissionError("ADMISSION_CONFLICT", 409)

    def _persist_steer_without_task_if_steer(
        self, event: EventRequest, event_id: str
    ) -> None:
        if event.payload.get("delivery") == "steer":
            raise AssertionError("unreachable steer path")

    def _session_id_or_empty(
        self, connection: sqlite3.Connection, event: EventRequest
    ) -> str:
        found = sessions_repo.find_session_tree(
            connection, event.adapter, event.agent_session_id
        )
        return found.session_id if found else ""

    def _reroute_after_race_locked(
        self,
        connection: sqlite3.Connection,
        event: EventRequest,
        session_id: str,
        tree_id: str,
    ) -> tuple[str, str] | None:
        fresh = tasks_repo.find_running_task_by_session(connection, session_id)
        if fresh is not None:
            owner = tasks_repo.get_task_owner(connection, fresh)
            if decisions.is_owned_running_task(
                decisions.TaskOwnership(
                    session_id=owner.session_id,
                    tree_id=owner.tree_id,
                    status=owner.status,
                )
                if owner is not None
                else None,
                session_id,
                tree_id,
            ):
                return ("join", fresh)
        blocking = tasks_repo.find_active_task_by_tree(connection, tree_id)
        if blocking is not None:
            return ("overlap", blocking.id)
        return None

    def _expire_candidate_silent(self, candidate_id: str) -> None:
        try:
            with connect(self._database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                found = candidate_support.supersede_candidate_silent_locked(
                    connection,
                    candidate_id,
                    candidate_support.CANDIDATE_REROUTE_CODE,
                )
                if not found:
                    connection.rollback()
                    return
                connection.commit()
        except sqlite3.IntegrityError:
            pass

    def _replay_semantic_transactional(
        self,
        event: EventRequest,
        event_id: str,
        payload_hash: str,
        git_root: str,
    ) -> dict[str, object] | None:
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = admissions_repo.find_event(connection, event_id)
            if existing:
                connection.rollback()
                return self._reconcile_existing(
                    event_id, payload_hash, existing, event
                )
            tree_id = sessions_repo.get_working_tree_id(connection, git_root)
            found = sessions_repo.find_session_tree(
                connection, event.adapter, event.agent_session_id
            )
            if found is not None and decisions.is_session_tree_mismatch(
                found.tree_id, tree_id
            ):
                connection.rollback()
                raise AdmissionError("SESSION_WORKTREE_MISMATCH", 409)
            replayed = self._replay_semantic_locked(
                connection, event, event_id, payload_hash
            )
            if replayed is not None:
                connection.rollback()
                return replayed
            connection.rollback()
            return None

    def _replay_semantic_locked(
        self,
        connection: sqlite3.Connection,
        event: EventRequest,
        event_id: str,
        payload_hash: str,
    ) -> dict[str, object] | None:
        try:
            classification, stored = replay_support.classify_semantic_locked(
                connection, event
            )
            if classification == "none":
                return None
            if classification == "overlap":
                connection.rollback()
                return self._replay_overlap_decision(
                    event, event_id, payload_hash
                )
            if classification == "steer":
                connection.rollback()
                return self._replay_steer_rejection_inner(event, event_id)
            assert stored is not None
            try:
                replay_support.record_admitted_input_locked(
                    connection,
                    event=event,
                    event_id=event_id,
                    transport_hash=payload_hash,
                    stored=stored,
                    takeover=False,
                    now=utc_now_iso(),
                )
                connection.commit()
            except sqlite3.IntegrityError:
                connection.rollback()
                with connect(self._database_path) as fresh_conn:
                    existing = admissions_repo.find_event(fresh_conn, event_id)
                return self._reconcile_existing(
                    event_id, payload_hash, existing, event
                )
            return event_response(
                event_id, "accepted", "admitted", stored.id, stored.task_id
            ).model_dump()
        except AdmissionError:
            raise
        except sqlite3.IntegrityError:
            connection.rollback()
            with connect(self._database_path) as fresh_conn:
                existing = admissions_repo.find_event(fresh_conn, event_id)
            return self._reconcile_existing(
                event_id, payload_hash, existing, event
            )

    def _replay_steer_rejection_inner(
        self, event: EventRequest, event_id: str
    ) -> dict[str, object]:
        raise AdmissionError("STEER_WITHOUT_ACTIVE_TASK", 409)

    def _semantic_join_conflict(
        self, event: EventRequest, event_id: str, payload_hash: str
    ) -> dict[str, object]:
        with connect(self._database_path) as connection:
            decision = admissions_repo.find_no_input_decision(
                connection,
                event.adapter,
                event.agent_session_id,
                event.input_id,
            )
            if decision and decision.admission_hash == self._admission_hash(
                event
            ):
                if (
                    decisions.decide_no_input_outcome(decision.outcome)
                    == "released_overlap"
                ):
                    return self._replay_overlap_decision(
                        event, event_id, payload_hash
                    )
                return self._replay_steer_rejection(
                    event, event_id, payload_hash
                )
        raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)

    def _replay_overlap_decision(
        self, event: EventRequest, event_id: str, payload_hash: str
    ) -> dict[str, object]:
        try:
            with connect(self._database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = admissions_repo.find_event(connection, event_id)
                if existing:
                    if not self._stored_matches(event, payload_hash, existing):
                        connection.rollback()
                        raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
                    if existing.status != "processing":
                        connection.rollback()
                        return self._reconcile_existing(
                            event_id, payload_hash, existing, event
                        )
                    replay_support.record_overlap_locked(
                        connection,
                        event=event,
                        event_id=event_id,
                        transport_hash=payload_hash,
                        takeover=True,
                        now=utc_now_iso(),
                    )
                    connection.commit()
                    return event_response(
                        event_id, "accepted", "released_overlap", None, None
                    ).model_dump()
                replay_support.record_overlap_locked(
                    connection,
                    event=event,
                    event_id=event_id,
                    transport_hash=payload_hash,
                    takeover=False,
                    now=utc_now_iso(),
                )
                connection.commit()
        except sqlite3.IntegrityError:
            with connect(self._database_path) as connection:
                existing = admissions_repo.find_event(connection, event_id)
            return self._reconcile_existing(
                event_id, payload_hash, existing, event
            )
        return event_response(
            event_id, "accepted", "released_overlap", None, None
        ).model_dump()

    def _replay_steer_rejection(
        self, event: EventRequest, event_id: str, payload_hash: str
    ) -> dict[str, object]:
        try:
            with connect(self._database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = admissions_repo.find_event(connection, event_id)
                if existing:
                    if not self._stored_matches(event, payload_hash, existing):
                        connection.rollback()
                        raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
                    if existing.status != "processing":
                        connection.rollback()
                        return self._reconcile_existing(
                            event_id, payload_hash, existing, event
                        )
                    replay_support.record_steer_locked(
                        connection,
                        event=event,
                        event_id=event_id,
                        transport_hash=payload_hash,
                        takeover=True,
                        now=utc_now_iso(),
                    )
                    connection.commit()
                    logger.warning(
                        "admission rejected code=STEER_WITHOUT_ACTIVE_TASK "
                        "event_id=%s",
                        event_id,
                    )
                    raise AdmissionError("STEER_WITHOUT_ACTIVE_TASK", 409)
                replay_support.record_steer_locked(
                    connection,
                    event=event,
                    event_id=event_id,
                    transport_hash=payload_hash,
                    takeover=False,
                    now=utc_now_iso(),
                )
                connection.commit()
        except sqlite3.IntegrityError:
            with connect(self._database_path) as connection:
                existing = admissions_repo.find_event(connection, event_id)
            self._reconcile_existing(event_id, payload_hash, existing, event)
            raise AdmissionError("STEER_WITHOUT_ACTIVE_TASK", 409) from None
        logger.warning(
            "admission rejected code=STEER_WITHOUT_ACTIVE_TASK event_id=%s",
            event_id,
        )
        raise AdmissionError("STEER_WITHOUT_ACTIVE_TASK", 409)

    def _persist_released_overlap(
        self,
        event: EventRequest,
        event_id: str,
        payload_hash: str,
        blocking_task_id: str,
        now: str,
        git_root: str,
        deadline: float | None = None,
        depth: int = 0,
    ) -> dict[str, object]:
        if depth >= _MAX_ROUTE_ATTEMPTS:
            raise AdmissionError("ADMISSION_CONFLICT", 409)
        fenced_keys: list[tuple[str, int]] = []
        try:
            # Fence + decision persist in the same DB transaction;
            # the shared boundary lock keeps begin->registry and
            # fence->snapshot atomic. Cancel/reap happens after
            # commit, still before the HTTP response; the DB fence
            # stays authoritative if cancellation fails.
            with self._runner.boundary_lock:
                with connect(self._database_path) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    existing = admissions_repo.find_event(connection, event_id)
                    takeover_processing = False
                    if existing:
                        if not self._stored_matches(
                            event, payload_hash, existing
                        ):
                            connection.rollback()
                            raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
                        if existing.status != "processing":
                            connection.rollback()
                            return self._reconcile_existing(
                                event_id, payload_hash, existing, event
                            )
                        takeover_processing = True
                    tree_id = sessions_repo.get_working_tree_id(
                        connection, git_root
                    )
                    self._check_session_tree_mismatch(
                        connection, event, tree_id
                    )
                    decision = admissions_repo.find_no_input_decision(
                        connection,
                        event.adapter,
                        event.agent_session_id,
                        event.input_id,
                    )
                    if decision:
                        connection.rollback()
                        if decision.admission_hash != self._admission_hash(
                            event
                        ):
                            raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
                        return self._replay_overlap_decision(
                            event, event_id, payload_hash
                        )
                    stored_input = tasks_repo.find_input_by_adapter_session(
                        connection,
                        event.adapter,
                        event.agent_session_id,
                        event.input_id,
                    )
                    if stored_input:
                        connection.rollback()
                        if stored_input.admission_hash != self._admission_hash(
                            event
                        ):
                            raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
                        stored_owner = tasks_repo.get_task_owner(
                            connection, stored_input.task_id
                        )
                        self._require_execution_match(
                            stored_owner.execution_id
                            if stored_owner
                            else None,
                            event,
                        )
                        return self._replay_admitted_input(
                            event, event_id, payload_hash, stored_input
                        )
                    fresh_blocking = (
                        tasks_repo.find_active_task_by_tree(
                            connection, tree_id
                        )
                        if tree_id is not None
                        else None
                    )
                    if fresh_blocking is None:
                        connection.rollback()
                        return self._route_without_joinable(
                            event,
                            event_id,
                            payload_hash,
                            now,
                            git_root,
                            deadline,
                            depth + 1,
                        )
                    self._fence_unfrozen_finalization_locked(
                        connection, tree_id, now
                    )
                    try:
                        admissions_repo.insert_no_input_decision(
                            connection,
                            NewNoInputDecision(
                                adapter=event.adapter,
                                agent_session_id=event.agent_session_id,
                                native_input_id=event.input_id,
                                admission_hash=self._admission_hash(event),
                                outcome="released_overlap",
                                event_id=event_id,
                                reference_task_id=fresh_blocking.id,
                                created_at=now,
                            ),
                        )
                        if takeover_processing:
                            admissions_repo.mark_event_accepted_overlap(
                                connection, event_id
                            )
                        else:
                            admissions_repo.insert_accepted_overlap_event(
                                connection,
                                event_id,
                                payload_hash,
                                event.event_type,
                                now,
                                self._payload_hash(event),
                            )
                        connection.commit()
                        if tree_id is not None:
                            fenced_keys = self._runner.snapshot_tree_keys(
                                tree_id
                            )
                    except sqlite3.IntegrityError:
                        connection.rollback()
                        with connect(self._database_path) as fresh_conn:
                            existing = admissions_repo.find_event(
                                fresh_conn, event_id
                            )
                            if existing:
                                return self._reconcile_existing(
                                    event_id,
                                    payload_hash,
                                    existing,
                                    event,
                                )
                            decision = admissions_repo.find_no_input_decision(
                                fresh_conn,
                                event.adapter,
                                event.agent_session_id,
                                event.input_id,
                            )
                        if decision:
                            if decision.admission_hash != self._admission_hash(
                                event
                            ):
                                raise AdmissionError(
                                    "IDEMPOTENCY_CONFLICT", 409
                                )
                            return self._replay_overlap_decision(
                                event, event_id, payload_hash
                            )
                        raise
            if fenced_keys:
                self._cancel_tree_workers_best_effort(fenced_keys)
            return event_response(
                event_id, "accepted", "released_overlap", None, None
            ).model_dump()
        except AdmissionError:
            raise
        except sqlite3.IntegrityError:
            logger.warning(
                "admission conflict code=ADMISSION_CONFLICT event_id=%s",
                event_id,
            )
            return self._reject_event(
                event, event_id, payload_hash, "ADMISSION_CONFLICT"
            )

    def _persist_steer_without_task(
        self,
        event: EventRequest,
        event_id: str,
        payload_hash: str,
        now: str,
        git_root: str,
        deadline: float | None = None,
        depth: int = 0,
    ) -> dict[str, object] | None:
        if depth >= _MAX_ROUTE_ATTEMPTS:
            raise AdmissionError("ADMISSION_CONFLICT", 409)
        fenced_keys: list[tuple[str, int]] = []
        with self._runner.boundary_lock:
            with connect(self._database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if self._race_hook is not None:
                    hook = self._race_hook
                    connection.commit()
                    try:
                        hook(self._database_path, event, None)
                    finally:
                        connection.execute("BEGIN IMMEDIATE")
                existing = admissions_repo.find_event(connection, event_id)
                takeover_processing = False
                if existing:
                    if not self._stored_matches(event, payload_hash, existing):
                        connection.rollback()
                        raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
                    if existing.status != "processing":
                        connection.rollback()
                        self._reconcile_existing(
                            event_id, payload_hash, existing, event
                        )
                        raise AdmissionError("STEER_WITHOUT_ACTIVE_TASK", 409)
                    takeover_processing = True
                tree_id = sessions_repo.get_working_tree_id(
                    connection, git_root
                )
                self._check_session_tree_mismatch(connection, event, tree_id)
                decision = admissions_repo.find_no_input_decision(
                    connection,
                    event.adapter,
                    event.agent_session_id,
                    event.input_id,
                )
                if decision:
                    connection.rollback()
                    if decision.admission_hash != self._admission_hash(event):
                        raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
                    if decisions.decide_no_input_outcome(decision.outcome) == (
                        "released_overlap"
                    ):
                        return self._replay_overlap_decision(
                            event, event_id, payload_hash
                        )
                    self._replay_steer_rejection(event, event_id, payload_hash)
                    raise AdmissionError("STEER_WITHOUT_ACTIVE_TASK", 409)
                stored_input = tasks_repo.find_input_by_adapter_session(
                    connection,
                    event.adapter,
                    event.agent_session_id,
                    event.input_id,
                )
                if stored_input:
                    connection.rollback()
                    if stored_input.admission_hash != self._admission_hash(
                        event
                    ):
                        raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
                    stored_owner = tasks_repo.get_task_owner(
                        connection, stored_input.task_id
                    )
                    self._require_execution_match(
                        stored_owner.execution_id if stored_owner else None,
                        event,
                    )
                    return self._replay_admitted_input(
                        event, event_id, payload_hash, stored_input
                    )
                session_row = sessions_repo.find_session_tree(
                    connection, event.adapter, event.agent_session_id
                )
                if session_row is not None and tree_id is not None:
                    joinable = tasks_repo.find_running_task_by_session(
                        connection, session_row.session_id
                    )
                    if joinable is not None:
                        owner = tasks_repo.get_task_owner(connection, joinable)
                        if decisions.is_owned_running_task(
                            decisions.TaskOwnership(
                                session_id=owner.session_id,
                                tree_id=owner.tree_id,
                                status=owner.status,
                            )
                            if owner is not None
                            else None,
                            session_row.session_id,
                            tree_id,
                        ):
                            connection.rollback()
                            return self._join_active_task(
                                event,
                                event_id,
                                payload_hash,
                                joinable,
                                now,
                                git_root,
                                deadline,
                                depth + 1,
                            )
                self._fence_unfrozen_finalization_locked(
                    connection, tree_id, now
                )
                try:
                    admissions_repo.insert_no_input_decision(
                        connection,
                        NewNoInputDecision(
                            adapter=event.adapter,
                            agent_session_id=event.agent_session_id,
                            native_input_id=event.input_id,
                            admission_hash=self._admission_hash(event),
                            outcome="steer_without_active_task",
                            event_id=event_id,
                            reference_task_id=None,
                            created_at=now,
                        ),
                    )
                    if takeover_processing:
                        admissions_repo.mark_event_rejected(
                            connection, event_id, "STEER_WITHOUT_ACTIVE_TASK"
                        )
                    else:
                        admissions_repo.insert_rejected_event(
                            connection,
                            event_id,
                            payload_hash,
                            event.event_type,
                            now,
                            "STEER_WITHOUT_ACTIVE_TASK",
                            self._payload_hash(event),
                        )
                    connection.commit()
                    if tree_id is not None:
                        fenced_keys = self._runner.snapshot_tree_keys(tree_id)
                except sqlite3.IntegrityError:
                    connection.rollback()
                    with connect(self._database_path) as fresh_conn:
                        existing = admissions_repo.find_event(
                            fresh_conn, event_id
                        )
                        if existing:
                            self._reconcile_existing(
                                event_id, payload_hash, existing, event
                            )
                            raise AdmissionError(
                                "STEER_WITHOUT_ACTIVE_TASK", 409
                            ) from None
                        decision = admissions_repo.find_no_input_decision(
                            fresh_conn,
                            event.adapter,
                            event.agent_session_id,
                            event.input_id,
                        )
                    if decision:
                        if decision.admission_hash != self._admission_hash(
                            event
                        ):
                            raise AdmissionError(
                                "IDEMPOTENCY_CONFLICT", 409
                            ) from None
                        if (
                            decisions.decide_no_input_outcome(decision.outcome)
                            == "released_overlap"
                        ):
                            return self._replay_overlap_decision(
                                event, event_id, payload_hash
                            )
                        self._replay_steer_rejection(
                            event, event_id, payload_hash
                        )
                    logger.warning(
                        "admission conflict "
                        "code=ADMISSION_CONFLICT event_id=%s",
                        event_id,
                    )
                    raise AdmissionError("ADMISSION_CONFLICT", 409) from None
        if fenced_keys:
            self._cancel_tree_workers_best_effort(fenced_keys)
        logger.warning(
            "admission rejected code=STEER_WITHOUT_ACTIVE_TASK event_id=%s",
            event_id,
        )
        raise AdmissionError("STEER_WITHOUT_ACTIVE_TASK", 409)

    def _replay_admitted_input(
        self,
        event: EventRequest,
        event_id: str,
        payload_hash: str,
        stored_input: StoredInput,
    ) -> dict[str, object]:
        try:
            with connect(self._database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = admissions_repo.find_event(connection, event_id)
                if existing:
                    if not self._stored_matches(event, payload_hash, existing):
                        connection.rollback()
                        raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
                    if existing.status != "processing":
                        connection.rollback()
                        return self._reconcile_existing(
                            event_id, payload_hash, existing, event
                        )
                    replay_support.record_admitted_input_locked(
                        connection,
                        event=event,
                        event_id=event_id,
                        transport_hash=payload_hash,
                        stored=stored_input,
                        takeover=True,
                        now=utc_now_iso(),
                    )
                    connection.commit()
                    return event_response(
                        event_id,
                        "accepted",
                        "admitted",
                        stored_input.id,
                        stored_input.task_id,
                    ).model_dump()
                replay_support.record_admitted_input_locked(
                    connection,
                    event=event,
                    event_id=event_id,
                    transport_hash=payload_hash,
                    stored=stored_input,
                    takeover=False,
                    now=utc_now_iso(),
                )
                connection.commit()
        except sqlite3.IntegrityError:
            with connect(self._database_path) as connection:
                existing = admissions_repo.find_event(connection, event_id)
            return self._reconcile_existing(
                event_id, payload_hash, existing, event
            )
        return event_response(
            event_id,
            "accepted",
            "admitted",
            stored_input.id,
            stored_input.task_id,
        ).model_dump()

    def _payload_hash(self, event: EventRequest) -> str:
        return replay_support.payload_hash(event)

    def _stored_matches(
        self,
        event: EventRequest,
        incoming_transport: str,
        row: InboundEvent,
    ) -> bool:
        return replay_support.stored_matches(event, incoming_transport, row)

    def _admission_hash(self, event: EventRequest) -> str:
        return replay_support.admission_hash(event)

    def _reconcile_existing(
        self,
        event_id: str,
        payload_hash: str,
        row: InboundEvent | None,
        event: EventRequest,
    ) -> dict[str, object]:
        return replay_support.reconcile_existing(
            event_id, payload_hash, row, event
        )

    def reconcile_incomplete(self) -> None:
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            candidate_support.expire_captured_for_recovery_locked(connection)
            connection.commit()
