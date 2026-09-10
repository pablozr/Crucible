from __future__ import annotations

import sqlite3

from crucible_core.schemas.persistence import (
    BaselineFileRow,
    CandidateRef,
    CapturedCandidate,
    EventSummary,
    InboundEvent,
    InboundEventDetail,
    NewAcceptedEvent,
    NewAdmissionCandidate,
    NewNoInputDecision,
    NoInputDecisionRow,
)


def _has_semantic_hash(connection: sqlite3.Connection) -> bool:
    saved = connection.row_factory
    try:
        connection.row_factory = None
        rows = connection.execute(
            "PRAGMA table_info(inbound_events)"
        ).fetchall()
    finally:
        connection.row_factory = saved
    return any(row[1] == "semantic_hash" for row in rows)


def find_event(
    connection: sqlite3.Connection, event_id: str
) -> InboundEvent | None:
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT payload_hash AS payload_hash, "
            "semantic_hash AS semantic_hash, status AS status, "
            "outcome AS outcome, input_id AS input_id, task_id AS task_id, "
            "failure_code AS failure_code "
            "FROM inbound_events WHERE id = ?",
            (event_id,),
        ).fetchone()
    except sqlite3.OperationalError as error:
        if "semantic_hash" not in str(error):
            raise
        row = connection.execute(
            "SELECT payload_hash AS payload_hash, status AS status, "
            "outcome AS outcome, input_id AS input_id, task_id AS task_id, "
            "failure_code AS failure_code "
            "FROM inbound_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        return InboundEvent(
            payload_hash=row["payload_hash"],
            semantic_hash=None,
            status=row["status"],
            outcome=row["outcome"],
            input_id=row["input_id"],
            task_id=row["task_id"],
            failure_code=row["failure_code"],
        )
    if row is None:
        return None

    return InboundEvent(
        payload_hash=row["payload_hash"],
        semantic_hash=row["semantic_hash"],
        status=row["status"],
        outcome=row["outcome"],
        input_id=row["input_id"],
        task_id=row["task_id"],
        failure_code=row["failure_code"],
    )


def find_event_detail(
    connection: sqlite3.Connection, event_id: str
) -> InboundEventDetail | None:
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT status AS status, outcome AS outcome, "
            "input_id AS input_id, task_id AS task_id, "
            "payload_hash AS payload_hash, "
            "semantic_hash AS semantic_hash, "
            "failure_code AS failure_code "
            "FROM inbound_events WHERE id = ?",
            (event_id,),
        ).fetchone()
    except sqlite3.OperationalError as error:
        if "semantic_hash" not in str(error):
            raise
        row = connection.execute(
            "SELECT status AS status, outcome AS outcome, "
            "input_id AS input_id, task_id AS task_id, "
            "payload_hash AS payload_hash, "
            "failure_code AS failure_code "
            "FROM inbound_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        return InboundEventDetail(
            status=row["status"],
            outcome=row["outcome"],
            input_id=row["input_id"],
            task_id=row["task_id"],
            payload_hash=row["payload_hash"],
            semantic_hash=None,
            failure_code=row["failure_code"],
        )
    if row is None:
        return None

    return InboundEventDetail(
        status=row["status"],
        outcome=row["outcome"],
        input_id=row["input_id"],
        task_id=row["task_id"],
        payload_hash=row["payload_hash"],
        semantic_hash=row["semantic_hash"],
        failure_code=row["failure_code"],
    )


def list_events(
    connection: sqlite3.Connection, limit: int
) -> list[EventSummary]:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT id AS event_id, status AS status, outcome AS outcome, "
        "failure_code AS failure_code FROM inbound_events "
        "ORDER BY received_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()

    return [
        EventSummary(
            event_id=row["event_id"],
            status=row["status"],
            outcome=row["outcome"],
            failure_code=row["failure_code"],
        )
        for row in rows
    ]


def insert_processing_event(
    connection: sqlite3.Connection,
    event_id: str,
    payload_hash: str,
    event_type: str,
    received_at: str,
    semantic_hash: str | None = None,
) -> None:
    if _has_semantic_hash(connection):
        connection.execute(
            "INSERT INTO inbound_events "
            "(id, payload_hash, semantic_hash, status, event_type, "
            "received_at, outcome) "
            "VALUES (?, ?, ?, 'processing', ?, ?, 'candidate')",
            (
                event_id,
                payload_hash,
                semantic_hash,
                event_type,
                received_at,
            ),
        )
        return
    connection.execute(
        "INSERT INTO inbound_events "
        "(id, payload_hash, status, event_type, received_at, outcome) "
        "VALUES (?, ?, 'processing', ?, ?, 'candidate')",
        (event_id, payload_hash, event_type, received_at),
    )


def insert_accepted_event(
    connection: sqlite3.Connection, event: NewAcceptedEvent
) -> None:
    if _has_semantic_hash(connection):
        connection.execute(
            "INSERT INTO inbound_events (id, payload_hash, semantic_hash, "
            "status, event_type, received_at, outcome, input_id, task_id) "
            "VALUES (?, ?, ?, 'accepted', ?, ?, 'admitted', ?, ?)",
            (
                event.event_id,
                event.payload_hash,
                event.semantic_hash,
                event.event_type,
                event.received_at,
                event.input_id,
                event.task_id,
            ),
        )
        return
    connection.execute(
        "INSERT INTO inbound_events (id, payload_hash, status, "
        "event_type, received_at, outcome, input_id, task_id) "
        "VALUES (?, ?, 'accepted', ?, ?, 'admitted', ?, ?)",
        (
            event.event_id,
            event.payload_hash,
            event.event_type,
            event.received_at,
            event.input_id,
            event.task_id,
        ),
    )


def insert_accepted_overlap_event(
    connection: sqlite3.Connection,
    event_id: str,
    payload_hash: str,
    event_type: str,
    received_at: str,
    semantic_hash: str | None = None,
) -> None:
    if _has_semantic_hash(connection):
        connection.execute(
            "INSERT INTO inbound_events (id, payload_hash, semantic_hash, "
            "status, event_type, received_at, outcome) "
            "VALUES (?, ?, ?, 'accepted', ?, ?, 'released_overlap')",
            (
                event_id,
                payload_hash,
                semantic_hash,
                event_type,
                received_at,
            ),
        )
        return
    connection.execute(
        "INSERT INTO inbound_events (id, payload_hash, status, "
        "event_type, received_at, outcome) "
        "VALUES (?, ?, 'accepted', ?, ?, 'released_overlap')",
        (event_id, payload_hash, event_type, received_at),
    )


def insert_rejected_event(
    connection: sqlite3.Connection,
    event_id: str,
    payload_hash: str,
    event_type: str,
    received_at: str,
    code: str,
    semantic_hash: str | None = None,
) -> None:
    if _has_semantic_hash(connection):
        connection.execute(
            "INSERT INTO inbound_events "
            "(id, payload_hash, semantic_hash, status, event_type, "
            "received_at, outcome, failure_code, failure_message) "
            "VALUES (?, ?, ?, 'rejected', ?, ?, 'rejected', ?, ?)",
            (
                event_id,
                payload_hash,
                semantic_hash,
                event_type,
                received_at,
                code,
                code,
            ),
        )
        return
    connection.execute(
        "INSERT INTO inbound_events "
        "(id, payload_hash, status, event_type, "
        "received_at, outcome, failure_code, failure_message) "
        "VALUES (?, ?, 'rejected', ?, ?, 'rejected', ?, ?)",
        (event_id, payload_hash, event_type, received_at, code, code),
    )


def mark_event_accepted(
    connection: sqlite3.Connection,
    event_id: str,
    input_id: str,
    task_id: str | None,
) -> None:
    connection.execute(
        "UPDATE inbound_events SET status = 'accepted', "
        "outcome = 'admitted', "
        "input_id = ?, task_id = ? WHERE id = ?",
        (input_id, task_id, event_id),
    )


def mark_event_accepted_overlap(
    connection: sqlite3.Connection, event_id: str
) -> None:
    connection.execute(
        "UPDATE inbound_events SET status = 'accepted', "
        "outcome = 'released_overlap', input_id = NULL, task_id = NULL "
        "WHERE id = ?",
        (event_id,),
    )


def mark_event_rejected(
    connection: sqlite3.Connection, event_id: str, code: str
) -> None:
    connection.execute(
        "UPDATE inbound_events SET status = 'rejected', "
        "outcome = 'rejected', "
        "failure_code = ?, failure_message = ? WHERE id = ?",
        (code, code, event_id),
    )


def expire_processing_events(connection: sqlite3.Connection) -> None:
    connection.execute(
        "UPDATE inbound_events SET status = 'rejected', "
        "outcome = 'rejected', "
        "failure_code = 'CANDIDATE_EXPIRED', "
        "failure_message = 'CANDIDATE_EXPIRED' "
        "WHERE status = 'processing' AND event_type = 'input_candidate'"
    )


def insert_candidate(
    connection: sqlite3.Connection, candidate: NewAdmissionCandidate
) -> None:
    connection.execute(
        "INSERT INTO admission_candidates "
        "(id, session_id, native_input_id, "
        "status, baseline_head, baseline_status, baseline_branch, "
        "baseline_index_manifest, outcome, created_at, admission_hash, "
        "event_id) VALUES (?, ?, ?, 'captured', ?, ?, ?, ?, "
        "'candidate', ?, ?, ?)",
        (
            candidate.candidate_id,
            candidate.session_id,
            candidate.native_input_id,
            candidate.baseline_head,
            candidate.baseline_status,
            candidate.baseline_branch,
            candidate.baseline_index_manifest,
            candidate.created_at,
            candidate.admission_hash,
            candidate.event_id,
        ),
    )


def find_captured_candidate(
    connection: sqlite3.Connection, candidate_id: str
) -> CapturedCandidate | None:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT baseline_head AS baseline_head, "
        "baseline_status AS baseline_status, "
        "baseline_branch AS baseline_branch, "
        "baseline_index_manifest AS baseline_index_manifest "
        "FROM admission_candidates WHERE id = ? "
        "AND status = 'captured'",
        (candidate_id,),
    ).fetchone()
    if row is None:
        return None

    return CapturedCandidate(
        baseline_head=row["baseline_head"],
        baseline_status=row["baseline_status"],
        baseline_branch=row["baseline_branch"],
        baseline_index_manifest=row["baseline_index_manifest"],
    )


def list_candidate_ids_by_event(
    connection: sqlite3.Connection, event_id: str
) -> list[CandidateRef]:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT id AS candidate_id FROM admission_candidates "
        "WHERE event_id = ?",
        (event_id,),
    ).fetchall()

    return [CandidateRef(candidate_id=row["candidate_id"]) for row in rows]


def list_captured_candidate_ids(
    connection: sqlite3.Connection,
) -> list[CandidateRef]:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT id AS candidate_id FROM admission_candidates "
        "WHERE status = 'captured'"
    ).fetchall()

    return [CandidateRef(candidate_id=row["candidate_id"]) for row in rows]


def mark_candidate_promoted(
    connection: sqlite3.Connection, candidate_id: str, input_id: str
) -> None:
    connection.execute(
        "UPDATE admission_candidates SET status = 'promoted', "
        "outcome = "
        "'admitted', input_id = ? WHERE id = ?",
        (input_id, candidate_id),
    )


def mark_candidate_superseded(
    connection: sqlite3.Connection, candidate_id: str, code: str
) -> None:
    connection.execute(
        "UPDATE admission_candidates SET status = 'expired', "
        "outcome = 'superseded', failure_code = ?, "
        "failure_message = ? WHERE id = ?",
        (code, code, candidate_id),
    )


def mark_candidates_expired_by_event(
    connection: sqlite3.Connection, event_id: str, code: str
) -> None:
    connection.execute(
        "UPDATE admission_candidates SET status = 'expired', "
        "outcome = 'rejected', failure_code = ?, failure_message = ? "
        "WHERE event_id = ?",
        (code, code, event_id),
    )


def expire_captured_candidates(connection: sqlite3.Connection) -> None:
    connection.execute(
        "UPDATE admission_candidates SET status = 'expired', "
        "outcome = 'CANDIDATE_EXPIRED', "
        "failure_code = 'CANDIDATE_EXPIRED', "
        "failure_message = 'CANDIDATE_EXPIRED' "
        "WHERE status = 'captured'"
    )


def insert_candidate_file(
    connection: sqlite3.Connection,
    file_id: str,
    candidate_id: str,
    baseline_file: BaselineFileRow,
) -> None:
    connection.execute(
        "INSERT INTO candidate_baseline_files "
        "(id, candidate_id, path, status, sha256, size, is_binary, "
        "content) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            file_id,
            candidate_id,
            baseline_file.path,
            baseline_file.status,
            baseline_file.sha256,
            baseline_file.size,
            baseline_file.is_binary,
            baseline_file.content,
        ),
    )


def list_candidate_files(
    connection: sqlite3.Connection, candidate_id: str
) -> list[BaselineFileRow]:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT path AS path, status AS status, sha256 AS sha256, "
        "size AS size, is_binary AS is_binary, content AS content "
        "FROM candidate_baseline_files WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchall()

    return [
        BaselineFileRow(
            path=row["path"],
            status=row["status"],
            sha256=row["sha256"],
            size=row["size"],
            is_binary=row["is_binary"],
            content=row["content"],
        )
        for row in rows
    ]


def delete_candidate_files(
    connection: sqlite3.Connection, candidate_id: str
) -> None:
    connection.execute(
        "DELETE FROM candidate_baseline_files WHERE candidate_id = ?",
        (candidate_id,),
    )


def find_no_input_decision(
    connection: sqlite3.Connection,
    adapter: str,
    agent_session_id: str,
    native_input_id: str,
) -> NoInputDecisionRow | None:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT admission_hash AS admission_hash, outcome AS outcome, "
        "event_id AS event_id, reference_task_id AS reference_task_id "
        "FROM admission_no_input_decisions WHERE adapter = ? "
        "AND agent_session_id = ? AND native_input_id = ?",
        (adapter, agent_session_id, native_input_id),
    ).fetchone()
    if row is None:
        return None
    return NoInputDecisionRow(
        admission_hash=row["admission_hash"],
        outcome=row["outcome"],
        event_id=row["event_id"],
        reference_task_id=row["reference_task_id"],
    )


def insert_no_input_decision(
    connection: sqlite3.Connection, decision: NewNoInputDecision
) -> None:
    connection.execute(
        "INSERT INTO admission_no_input_decisions (adapter, "
        "agent_session_id, native_input_id, admission_hash, outcome, "
        "event_id, reference_task_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            decision.adapter,
            decision.agent_session_id,
            decision.native_input_id,
            decision.admission_hash,
            decision.outcome,
            decision.event_id,
            decision.reference_task_id,
            decision.created_at,
        ),
    )
