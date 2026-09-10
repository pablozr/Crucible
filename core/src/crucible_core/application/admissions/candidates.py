"""Locked candidate mutations for admissions.

Every helper runs inside a coordinator-owned transaction on the given
connection. This module never chooses routes, captures baselines or
opens connections; it only persists the candidate lifecycle.
"""

from __future__ import annotations

import sqlite3
import uuid

from crucible_core.repositories import admissions_repository as admissions_repo
from crucible_core.repositories import tasks_repository as tasks_repo
from crucible_core.schemas.git import BaselineCaptureSnapshot
from crucible_core.schemas.persistence import (
    BaselineFileRow,
    CapturedCandidate,
    NewAdmissionCandidate,
    NewStoredInput,
    NewTask,
)

CANDIDATE_REROUTE_CODE = "CANDIDATE_SUPERSEDED"


def persist_candidate_files_locked(
    connection: sqlite3.Connection,
    candidate_id: str,
    files: list[BaselineFileRow],
) -> None:
    for baseline_file in files:
        admissions_repo.insert_candidate_file(
            connection, str(uuid.uuid4()), candidate_id, baseline_file
        )


def insert_processing_candidate_locked(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    payload_hash: str,
    semantic_hash: str,
    event_type: str,
    native_input_id: str,
    candidate_id: str,
    session_id: str,
    baseline: BaselineCaptureSnapshot,
    created_at: str,
    admission_hash: str,
) -> None:
    admissions_repo.insert_processing_event(
        connection,
        event_id,
        payload_hash,
        event_type,
        created_at,
        semantic_hash,
    )
    admissions_repo.insert_candidate(
        connection,
        NewAdmissionCandidate(
            candidate_id=candidate_id,
            session_id=session_id,
            native_input_id=native_input_id,
            baseline_head=baseline.head,
            baseline_status=baseline.status,
            baseline_branch=baseline.branch,
            baseline_index_manifest=baseline.index,
            created_at=created_at,
            admission_hash=admission_hash,
            event_id=event_id,
        ),
    )
    persist_candidate_files_locked(connection, candidate_id, baseline.files)


def promote_candidate_locked(
    connection: sqlite3.Connection,
    *,
    candidate: CapturedCandidate,
    candidate_id: str,
    event_id: str,
    task_id: str,
    input_id: str,
    session_id: str,
    tree_id: str,
    execution_id: str,
    created_at: str,
    admission_hash: str,
    native_input_id: str,
) -> None:
    tasks_repo.insert_task(
        connection,
        NewTask(
            task_id=task_id,
            session_id=session_id,
            tree_id=tree_id,
            started_at=created_at,
            execution_id=execution_id,
            baseline_head=candidate.baseline_head,
            baseline_status=candidate.baseline_status,
            baseline_branch=candidate.baseline_branch,
            baseline_index_manifest=(candidate.baseline_index_manifest),
        ),
    )
    tasks_repo.insert_input(
        connection,
        NewStoredInput(
            row_id=input_id,
            session_id=session_id,
            task_id=task_id,
            input_id=native_input_id,
            admission_hash=admission_hash,
        ),
    )
    for baseline_file in admissions_repo.list_candidate_files(
        connection, candidate_id
    ):
        tasks_repo.insert_task_baseline_file(
            connection, str(uuid.uuid4()), task_id, baseline_file
        )
    admissions_repo.mark_candidate_promoted(connection, candidate_id, input_id)
    admissions_repo.delete_candidate_files(connection, candidate_id)
    admissions_repo.mark_event_accepted(
        connection, event_id, input_id, task_id
    )


def expire_event_candidates_locked(
    connection: sqlite3.Connection, event_id: str, code: str
) -> None:
    candidate_ids = admissions_repo.list_candidate_ids_by_event(
        connection, event_id
    )
    for candidate_ref in candidate_ids:
        admissions_repo.delete_candidate_files(
            connection, candidate_ref.candidate_id
        )
    admissions_repo.mark_candidates_expired_by_event(
        connection, event_id, code
    )
    admissions_repo.mark_event_rejected(connection, event_id, code)


def supersede_candidate_silent_locked(
    connection: sqlite3.Connection, candidate_id: str, code: str
) -> bool:
    row = connection.execute(
        "SELECT event_id FROM admission_candidates WHERE id = ?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        return False
    admissions_repo.delete_candidate_files(connection, candidate_id)
    admissions_repo.mark_candidate_superseded(connection, candidate_id, code)
    return True


def expire_captured_for_recovery_locked(
    connection: sqlite3.Connection,
) -> None:
    candidate_ids = admissions_repo.list_captured_candidate_ids(connection)
    admissions_repo.expire_captured_candidates(connection)
    admissions_repo.expire_processing_events(connection)
    for candidate in candidate_ids:
        admissions_repo.delete_candidate_files(
            connection, candidate.candidate_id
        )
