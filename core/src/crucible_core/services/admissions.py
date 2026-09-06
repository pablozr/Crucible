from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
import sqlite3
import subprocess
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from crucible_core.core.database import connect
from crucible_core.logging import get_logger
from crucible_core.repositories import admissions_repository as admissions_repo
from crucible_core.repositories import sessions_repository as sessions_repo
from crucible_core.repositories import tasks_repository as tasks_repo
from crucible_core.responses.admissions import (
    event_detail,
    event_response,
    reconciled_event_response,
    task_detail,
    task_summary,
)
from crucible_core.schemas.admissions import EventRequest
from crucible_core.schemas.persistence import (
    BaselineFileRow,
    InboundEvent,
    NewAcceptedEvent,
    NewAdmissionCandidate,
    NewNoInputDecision,
    NewStoredInput,
    NewTask,
    StoredInput,
)
from crucible_core.schemas.projects import Project
from crucible_core.services.projects import ProjectError, resolve_project
from crucible_core.utils.functions import (
    canonical_json_sha256,
    utc_now_iso,
)

CAPTURE_DEADLINE_SECONDS = 2
SUBPROCESS_TIMEOUT_SECONDS = 0.5

logger = get_logger(__name__)


class AdmissionError(ValueError):
    def __init__(self, code: str, status_code: int = 400) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def admit_event(database_path: Path, event: EventRequest) -> dict[str, object]:
    event_id = str(event.event_id)
    payload_hash = _payload_hash(event)
    now = utc_now_iso()
    deadline = time.monotonic() + CAPTURE_DEADLINE_SECONDS

    _validate_event(event)

    existing = _existing_outcome(
        database_path,
        event,
        event_id,
        payload_hash,
    )

    if existing:
        return existing

    try:
        project = _resolve_event_project(event)
        session_id, tree_id = _ensure_session(
            database_path, event, project.id, project.git_root
        )
        delivery = event.payload.get("delivery")

        with connect(database_path) as connection:
            active_same = tasks_repo.find_active_task_by_session(
                connection, session_id
            )

        if delivery == "steer":
            if active_same is None:
                replayed = _persist_steer_without_task(
                    database_path, event, event_id, payload_hash, now
                )
                if replayed is not None:
                    return replayed
            return _join_active_task(
                database_path,
                event,
                event_id,
                payload_hash,
                session_id,
                active_same,
                now,
            )

        if active_same is not None:
            return _join_active_task(
                database_path,
                event,
                event_id,
                payload_hash,
                session_id,
                active_same,
                now,
            )

        with connect(database_path) as connection:
            blocking = tasks_repo.find_active_task_by_tree(connection, tree_id)

        if blocking is not None:
            return _persist_released_overlap(
                database_path,
                event,
                event_id,
                payload_hash,
                blocking.id,
                now,
            )

        baseline = _capture_baseline(
            Path(project.git_root),
            project.max_snapshot_file_size_bytes,
            deadline,
        )
        candidate_id = str(uuid.uuid4())

        with connect(database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = admissions_repo.find_event(connection, event_id)
            if existing:
                connection.rollback()
                return _reconcile_existing(event_id, payload_hash, existing)
            session_id, tree_id = _persist_session(
                connection, event, project.id, project.git_root
            )
            stored_input = tasks_repo.find_input(
                connection, session_id, event.input_id
            )
            if stored_input:
                if stored_input.admission_hash != _admission_hash(event):
                    raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)

                admissions_repo.insert_accepted_event(
                    connection,
                    NewAcceptedEvent(
                        event_id=event_id,
                        payload_hash=payload_hash,
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
            admissions_repo.insert_processing_event(
                connection, event_id, payload_hash, event.event_type, now
            )
            admissions_repo.insert_candidate(
                connection,
                NewAdmissionCandidate(
                    candidate_id=candidate_id,
                    session_id=session_id,
                    native_input_id=event.input_id,
                    baseline_head=baseline["head"],
                    baseline_status=baseline["status"],
                    baseline_branch=baseline["branch"],
                    baseline_index_manifest=baseline["index"],
                    created_at=now,
                    admission_hash=_admission_hash(event),
                    event_id=event_id,
                ),
            )
            _persist_candidate_files(
                connection, candidate_id, baseline["files"]
            )
            connection.commit()
        return _promote_candidate(
            database_path,
            event,
            event_id,
            payload_hash,
            candidate_id,
            session_id,
            tree_id,
            now,
            deadline,
        )
    except AdmissionError as error:
        if error.code in (
            "IDEMPOTENCY_CONFLICT",
            "STEER_WITHOUT_ACTIVE_TASK",
        ):
            raise
        logger.warning(
            "admission rejected code=%s event_id=%s",
            error.code,
            event_id,
        )
        return _reject_event(
            database_path, event, event_id, payload_hash, error.code
        )
    except sqlite3.IntegrityError:
        logger.warning(
            "admission conflict code=ADMISSION_CONFLICT event_id=%s",
            event_id,
        )
        return _reject_event(
            database_path,
            event,
            event_id,
            payload_hash,
            "ADMISSION_CONFLICT",
        )


def _promote_candidate(
    database_path: Path,
    event: EventRequest,
    event_id: str,
    payload_hash: str,
    candidate_id: str,
    session_id: str,
    tree_id: str,
    now: str,
    deadline: float,
) -> dict[str, object]:
    try:
        with connect(database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            input_id = str(uuid.uuid4())
            task_id = str(uuid.uuid4())
            candidate = admissions_repo.find_captured_candidate(
                connection, candidate_id
            )
            if not candidate:
                existing = admissions_repo.find_event(connection, event_id)
                connection.rollback()
                return _reconcile_existing(event_id, payload_hash, existing)

            if time.monotonic() > deadline:
                connection.rollback()
                return _expire_candidate(database_path, event_id, payload_hash)
            tasks_repo.insert_task(
                connection,
                NewTask(
                    task_id=task_id,
                    session_id=session_id,
                    tree_id=tree_id,
                    started_at=now,
                    baseline_head=candidate.baseline_head,
                    baseline_status=candidate.baseline_status,
                    baseline_branch=candidate.baseline_branch,
                    baseline_index_manifest=(
                        candidate.baseline_index_manifest
                    ),
                ),
            )
            tasks_repo.insert_input(
                connection,
                NewStoredInput(
                    row_id=input_id,
                    session_id=session_id,
                    task_id=task_id,
                    input_id=event.input_id,
                    admission_hash=_admission_hash(event),
                ),
            )
            for baseline_file in admissions_repo.list_candidate_files(
                connection, candidate_id
            ):
                tasks_repo.insert_task_baseline_file(
                    connection, str(uuid.uuid4()), task_id, baseline_file
                )
            admissions_repo.mark_candidate_promoted(
                connection, candidate_id, input_id
            )
            admissions_repo.delete_candidate_files(connection, candidate_id)
            admissions_repo.mark_event_accepted(
                connection, event_id, input_id, task_id
            )
            connection.commit()
        return event_response(
            event_id, "accepted", "admitted", input_id, task_id
        ).model_dump()
    except sqlite3.IntegrityError:
        logger.warning(
            "admission conflict code=ADMISSION_CONFLICT event_id=%s",
            event_id,
        )
        return _expire_candidate(
            database_path,
            event_id,
            payload_hash,
            "ADMISSION_CONFLICT",
        )


def _reject_event(
    database_path: Path,
    event: EventRequest,
    event_id: str,
    payload_hash: str,
    code: str,
) -> dict[str, object]:
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = admissions_repo.find_event(connection, event_id)
        if row:
            if row.payload_hash != payload_hash:
                connection.rollback()
                raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
            if row.status != "processing":
                connection.rollback()
                return _reconcile_existing(event_id, payload_hash, row)
            admissions_repo.mark_event_rejected(connection, event_id, code)
        else:
            admissions_repo.insert_rejected_event(
                connection,
                event_id,
                payload_hash,
                event.event_type,
                utc_now_iso(),
                code,
            )
        connection.commit()
    return event_response(
        event_id, "rejected", "rejected", None, None
    ).model_dump()


def _expire_candidate(
    database_path: Path,
    event_id: str,
    payload_hash: str,
    code: str = "CANDIDATE_EXPIRED",
) -> dict[str, object]:
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        event = admissions_repo.find_event(connection, event_id)

        if not event or event.payload_hash != payload_hash:
            connection.rollback()
            raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)

        candidate_ids = admissions_repo.list_candidate_ids_by_event(
            connection, event_id
        )

        for candidate in candidate_ids:
            admissions_repo.delete_candidate_files(
                connection, candidate.candidate_id
            )

        admissions_repo.mark_candidates_expired_by_event(
            connection, event_id, code
        )
        admissions_repo.mark_event_rejected(connection, event_id, code)
        connection.commit()

    return event_response(
        event_id, "rejected", "rejected", None, None
    ).model_dump()


def get_event(database_path: Path, event_id: str) -> dict[str, object] | None:
    with connect(database_path) as connection:
        row = admissions_repo.find_event_detail(connection, event_id)
    if not row:
        return None
    return event_detail(event_id, row).model_dump()


def list_events(
    database_path: Path, limit: int, cursor: str | None
) -> dict[str, object]:
    with connect(database_path) as connection:
        rows = admissions_repo.list_events(connection, limit)
    return {"events": [row.model_dump() for row in rows]}


def list_tasks(
    database_path: Path, limit: int, cursor: str | None
) -> dict[str, object]:
    started_at: str | None = None
    cursor_task_id: str | None = None
    if cursor:
        try:
            started_at, cursor_task_id = json.loads(
                base64.urlsafe_b64decode(cursor).decode()
            )
        except (
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            binascii.Error,
        ):
            logger.warning("task list failed code=INVALID_CURSOR")
            raise AdmissionError("INVALID_CURSOR", 400) from None
        if not (
            isinstance(started_at, str)
            and isinstance(cursor_task_id, str)
            and started_at
            and cursor_task_id
        ):
            raise AdmissionError("INVALID_CURSOR", 400)
    with connect(database_path) as connection:
        rows = tasks_repo.list_tasks_page(
            connection, started_at, cursor_task_id, limit
        )
        task_ids = [row.id for row in rows[:limit]]
        input_rows = tasks_repo.list_inputs_by_task_ids(connection, task_ids)

    inputs: dict[str, list[str]] = {task_id: [] for task_id in task_ids}

    for link in input_rows:
        inputs[link.task_id].append(link.input_id)

    tasks = [
        {**task_summary(row).model_dump(), "input_ids": inputs[row.id]}
        for row in rows[:limit]
    ]
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps([last.started_at, last.id]).encode()
        ).decode()
    return {"tasks": tasks, "next_cursor": next_cursor}


def get_task(database_path: Path, task_id: str) -> dict[str, object] | None:
    with connect(database_path) as connection:
        row = tasks_repo.get_task_row(connection, task_id)
        if not row:
            return None
        inputs = [
            item.input_id
            for item in tasks_repo.list_input_ids_by_task(connection, task_id)
        ]
        files = tasks_repo.list_task_baseline_files(connection, task_id)
    return task_detail(row, inputs, files).model_dump()


def _capture_baseline(
    root: Path,
    max_size: int,
    deadline: float,
) -> dict[str, Any]:
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

    return {
        "head": head.decode().strip(),
        "branch": branch.decode().strip(),
        "status": _git(
            root,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            deadline,
        ),
        "index": _git(root, ["ls-files", "-s", "-z"], deadline),
    }


def _git(root: Path, arguments: list[str], deadline: float) -> bytes:
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


def _validate_event(event: EventRequest) -> None:
    if event.event_type != "input_candidate":
        raise AdmissionError("UNKNOWN_EVENT_TYPE")
    if event.payload_version != 1:
        raise AdmissionError("UNSUPPORTED_PAYLOAD_VERSION")
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


def _resolve_event_project(event: EventRequest) -> Project:
    try:
        project = resolve_project(event.git_root)
    except ProjectError as error:
        logger.warning("project validation failed code=%s", error)
        raise AdmissionError(str(error)) from error
    if str(event.project_id) != project.id:
        raise AdmissionError("PROJECT_ID_MISMATCH")
    return project


def _persist_session(
    connection: sqlite3.Connection,
    event: EventRequest,
    project_id: str,
    git_root: str,
) -> tuple[str, str]:
    tree_id = sessions_repo.get_working_tree_id(connection, git_root)
    if tree_id is None:
        tree_id = str(uuid.uuid4())
        sessions_repo.upsert_project(connection, project_id, git_root)
        sessions_repo.insert_working_tree(
            connection, tree_id, project_id, git_root
        )
    session_id = sessions_repo.find_session_id(
        connection, event.adapter, event.agent_session_id
    )
    if session_id is not None:
        return session_id, tree_id
    session_id = str(uuid.uuid4())
    sessions_repo.insert_session(
        connection,
        session_id,
        tree_id,
        event.adapter,
        event.agent_session_id,
        event.adapter_version,
        str(event.workspace_path.resolve()),
    )
    return session_id, tree_id


def _ensure_session(
    database_path: Path,
    event: EventRequest,
    project_id: str,
    git_root: str,
) -> tuple[str, str]:
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        tree_id = sessions_repo.get_working_tree_id(connection, git_root)
        if tree_id is None:
            tree_id = str(uuid.uuid4())
            sessions_repo.upsert_project(connection, project_id, git_root)
            sessions_repo.insert_working_tree(
                connection, tree_id, project_id, git_root
            )
        session_id = sessions_repo.find_session_id(
            connection, event.adapter, event.agent_session_id
        )
        if session_id is None:
            session_id = str(uuid.uuid4())
            sessions_repo.insert_session(
                connection,
                session_id,
                tree_id,
                event.adapter,
                event.agent_session_id,
                event.adapter_version,
                str(event.workspace_path.resolve()),
            )
        connection.commit()
    return session_id, tree_id


def _join_active_task(
    database_path: Path,
    event: EventRequest,
    event_id: str,
    payload_hash: str,
    session_id: str,
    active_task_id: str,
    now: str,
) -> dict[str, object]:
    try:
        with connect(database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = admissions_repo.find_event(connection, event_id)
            if existing:
                connection.rollback()
                return _reconcile_existing(event_id, payload_hash, existing)
            stored_input = tasks_repo.find_input(
                connection, session_id, event.input_id
            )
            if stored_input:
                if stored_input.admission_hash != _admission_hash(event):
                    raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
                admissions_repo.insert_accepted_event(
                    connection,
                    NewAcceptedEvent(
                        event_id=event_id,
                        payload_hash=payload_hash,
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
                return _semantic_join_conflict(database_path, event, event_id)
            input_id = str(uuid.uuid4())
            tasks_repo.insert_input(
                connection,
                NewStoredInput(
                    row_id=input_id,
                    session_id=session_id,
                    task_id=active_task_id,
                    input_id=event.input_id,
                    admission_hash=_admission_hash(event),
                ),
            )
            admissions_repo.insert_accepted_event(
                connection,
                NewAcceptedEvent(
                    event_id=event_id,
                    payload_hash=payload_hash,
                    event_type=event.event_type,
                    received_at=now,
                    input_id=input_id,
                    task_id=active_task_id,
                ),
            )
            connection.commit()
        return event_response(
            event_id, "accepted", "admitted", input_id, active_task_id
        ).model_dump()
    except AdmissionError:
        raise
    except sqlite3.IntegrityError:
        logger.warning(
            "admission conflict code=ADMISSION_CONFLICT event_id=%s",
            event_id,
        )
        return _reject_event(
            database_path, event, event_id, payload_hash, "ADMISSION_CONFLICT"
        )


def _semantic_join_conflict(
    database_path: Path, event: EventRequest, event_id: str
) -> dict[str, object]:
    with connect(database_path) as connection:
        decision = admissions_repo.find_no_input_decision(
            connection, event.adapter, event.agent_session_id, event.input_id
        )
        if decision and decision.admission_hash == _admission_hash(event):
            if decision.outcome == "released_overlap":
                return _replay_overlap_decision(database_path, event, event_id)
            return _replay_steer_rejection(database_path, event, event_id)
    raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)


def _replay_overlap_decision(
    database_path: Path, event: EventRequest, event_id: str
) -> dict[str, object]:
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = admissions_repo.find_event(connection, event_id)
        if existing:
            connection.rollback()
            return _reconcile_existing(
                event_id, _payload_hash(event), existing
            )
        admissions_repo.insert_accepted_overlap_event(
            connection,
            event_id,
            _payload_hash(event),
            event.event_type,
            utc_now_iso(),
        )
        connection.commit()
    return event_response(
        event_id, "accepted", "released_overlap", None, None
    ).model_dump()


def _replay_steer_rejection(
    database_path: Path, event: EventRequest, event_id: str
) -> dict[str, object]:
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = admissions_repo.find_event(connection, event_id)
        if existing:
            connection.rollback()
            return _reconcile_existing(
                event_id, _payload_hash(event), existing
            )
        admissions_repo.insert_rejected_event(
            connection,
            event_id,
            _payload_hash(event),
            event.event_type,
            utc_now_iso(),
            "STEER_WITHOUT_ACTIVE_TASK",
        )
        connection.commit()
    logger.warning(
        "admission rejected code=STEER_WITHOUT_ACTIVE_TASK event_id=%s",
        event_id,
    )
    raise AdmissionError("STEER_WITHOUT_ACTIVE_TASK", 409)


def _persist_released_overlap(
    database_path: Path,
    event: EventRequest,
    event_id: str,
    payload_hash: str,
    blocking_task_id: str,
    now: str,
) -> dict[str, object]:
    try:
        with connect(database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = admissions_repo.find_event(connection, event_id)
            if existing:
                connection.rollback()
                return _reconcile_existing(event_id, payload_hash, existing)
            decision = admissions_repo.find_no_input_decision(
                connection,
                event.adapter,
                event.agent_session_id,
                event.input_id,
            )
            if decision:
                connection.rollback()
                if decision.admission_hash != _admission_hash(event):
                    raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
                return _replay_overlap_decision(database_path, event, event_id)
            stored_input = tasks_repo.find_input_by_adapter_session(
                connection,
                event.adapter,
                event.agent_session_id,
                event.input_id,
            )
            if stored_input:
                connection.rollback()
                if stored_input.admission_hash != _admission_hash(event):
                    raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
                return _replay_admitted_input(
                    database_path, event, event_id, stored_input
                )
            admissions_repo.insert_no_input_decision(
                connection,
                NewNoInputDecision(
                    adapter=event.adapter,
                    agent_session_id=event.agent_session_id,
                    native_input_id=event.input_id,
                    admission_hash=_admission_hash(event),
                    outcome="released_overlap",
                    event_id=event_id,
                    reference_task_id=blocking_task_id,
                    created_at=now,
                ),
            )
            admissions_repo.insert_accepted_overlap_event(
                connection, event_id, payload_hash, event.event_type, now
            )
            connection.commit()
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
        return _reject_event(
            database_path, event, event_id, payload_hash, "ADMISSION_CONFLICT"
        )


def _persist_steer_without_task(
    database_path: Path,
    event: EventRequest,
    event_id: str,
    payload_hash: str,
    now: str,
) -> dict[str, object] | None:
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = admissions_repo.find_event(connection, event_id)
        if existing:
            connection.rollback()
            _reconcile_existing(event_id, payload_hash, existing)
            raise AdmissionError("STEER_WITHOUT_ACTIVE_TASK", 409)
        decision = admissions_repo.find_no_input_decision(
            connection,
            event.adapter,
            event.agent_session_id,
            event.input_id,
        )
        if decision:
            connection.rollback()
            if decision.admission_hash != _admission_hash(event):
                raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
            if decision.outcome == "released_overlap":
                return _replay_overlap_decision(database_path, event, event_id)
            _replay_steer_rejection(database_path, event, event_id)
            raise AdmissionError("STEER_WITHOUT_ACTIVE_TASK", 409)
        stored_input = tasks_repo.find_input_by_adapter_session(
            connection,
            event.adapter,
            event.agent_session_id,
            event.input_id,
        )
        if stored_input:
            connection.rollback()
            if stored_input.admission_hash != _admission_hash(event):
                raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
            return _replay_admitted_input(
                database_path, event, event_id, stored_input
            )
        try:
            admissions_repo.insert_no_input_decision(
                connection,
                NewNoInputDecision(
                    adapter=event.adapter,
                    agent_session_id=event.agent_session_id,
                    native_input_id=event.input_id,
                    admission_hash=_admission_hash(event),
                    outcome="steer_without_active_task",
                    event_id=event_id,
                    reference_task_id=None,
                    created_at=now,
                ),
            )
            admissions_repo.insert_rejected_event(
                connection,
                event_id,
                payload_hash,
                event.event_type,
                now,
                "STEER_WITHOUT_ACTIVE_TASK",
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            logger.warning(
                "admission conflict code=ADMISSION_CONFLICT event_id=%s",
                event_id,
            )
            raise AdmissionError("ADMISSION_CONFLICT", 409) from None
    logger.warning(
        "admission rejected code=STEER_WITHOUT_ACTIVE_TASK event_id=%s",
        event_id,
    )
    raise AdmissionError("STEER_WITHOUT_ACTIVE_TASK", 409)


def _replay_admitted_input(
    database_path: Path,
    event: EventRequest,
    event_id: str,
    stored_input: StoredInput,
) -> dict[str, object]:
    with connect(database_path) as connection:
        admissions_repo.insert_accepted_event(
            connection,
            NewAcceptedEvent(
                event_id=event_id,
                payload_hash=_payload_hash(event),
                event_type=event.event_type,
                received_at=utc_now_iso(),
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


def _payload_hash(event: EventRequest) -> str:
    payload = event.model_dump(mode="json", exclude_none=True)
    return canonical_json_sha256(payload)


def _admission_hash(event: EventRequest) -> str:
    payload = event.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"event_id", "occurred_at", "execution_id"},
    )
    return canonical_json_sha256(payload)


def _existing_outcome(
    path: Path, event: EventRequest, event_id: str, payload_hash: str
) -> dict[str, object] | None:
    with connect(path) as connection:
        row = admissions_repo.find_event(connection, event_id)
        if row:
            return _reconcile_existing(event_id, payload_hash, row)
        return _semantic_outcome(connection, event)


def _semantic_outcome(
    connection: sqlite3.Connection, event: EventRequest
) -> dict[str, object] | None:
    row = tasks_repo.find_input_by_adapter_session(
        connection, event.adapter, event.agent_session_id, event.input_id
    )
    if row:
        if row.admission_hash != _admission_hash(event):
            raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)

        admissions_repo.insert_accepted_event(
            connection,
            NewAcceptedEvent(
                event_id=str(event.event_id),
                payload_hash=_payload_hash(event),
                event_type=event.event_type,
                received_at=utc_now_iso(),
                input_id=row.id,
                task_id=row.task_id,
            ),
        )
        connection.commit()

        return event_response(
            str(event.event_id), "accepted", "admitted", row.id, row.task_id
        ).model_dump()

    decision = admissions_repo.find_no_input_decision(
        connection, event.adapter, event.agent_session_id, event.input_id
    )
    if not decision:
        return None
    if decision.admission_hash != _admission_hash(event):
        raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
    if decision.outcome == "released_overlap":
        admissions_repo.insert_accepted_overlap_event(
            connection,
            str(event.event_id),
            _payload_hash(event),
            event.event_type,
            utc_now_iso(),
        )
        connection.commit()
        return event_response(
            str(event.event_id), "accepted", "released_overlap", None, None
        ).model_dump()

    admissions_repo.insert_rejected_event(
        connection,
        str(event.event_id),
        _payload_hash(event),
        event.event_type,
        utc_now_iso(),
        "STEER_WITHOUT_ACTIVE_TASK",
    )
    connection.commit()
    logger.warning(
        "admission rejected code=STEER_WITHOUT_ACTIVE_TASK event_id=%s",
        event.event_id,
    )
    raise AdmissionError("STEER_WITHOUT_ACTIVE_TASK", 409)


def _reconcile_existing(
    event_id: str, payload_hash: str, row: InboundEvent | None
) -> dict[str, object]:
    if row is None or row.payload_hash != payload_hash:
        raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
    if row.failure_code == "STEER_WITHOUT_ACTIVE_TASK":
        raise AdmissionError("STEER_WITHOUT_ACTIVE_TASK", 409)
    return reconciled_event_response(event_id, row).model_dump()


def _persist_candidate_files(
    connection: sqlite3.Connection,
    candidate_id: str,
    files: list[BaselineFileRow],
) -> None:
    for baseline_file in files:
        admissions_repo.insert_candidate_file(
            connection, str(uuid.uuid4()), candidate_id, baseline_file
        )


def _file_identity(
    files: list[BaselineFileRow],
) -> list[tuple[str, str, str | None, int | None]]:
    return [(item.path, item.status, item.sha256, item.size) for item in files]


def reconcile_incomplete_admissions(database_path: Path) -> None:
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        candidate_ids = admissions_repo.list_captured_candidate_ids(connection)
        admissions_repo.expire_captured_candidates(connection)
        admissions_repo.expire_processing_events(connection)
        for candidate in candidate_ids:
            admissions_repo.delete_candidate_files(
                connection, candidate.candidate_id
            )
        connection.commit()
