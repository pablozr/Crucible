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


def find_event(
    connection: sqlite3.Connection, event_id: str
) -> InboundEvent | None:
    row = connection.execute(
        "SELECT payload_hash, status, outcome, input_id, task_id, "
        "failure_code "
        "FROM inbound_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        return None

    payload_hash, status, outcome, input_id, task_id, failure_code = row

    return InboundEvent(
        payload_hash=payload_hash,
        status=status,
        outcome=outcome,
        input_id=input_id,
        task_id=task_id,
        failure_code=failure_code,
    )


def find_event_detail(
    connection: sqlite3.Connection, event_id: str
) -> InboundEventDetail | None:
    row = connection.execute(
        "SELECT status, outcome, input_id, task_id, payload_hash, "
        "failure_code "
        "FROM inbound_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        return None

    status, outcome, input_id, task_id, payload_hash, failure_code = row

    return InboundEventDetail(
        status=status,
        outcome=outcome,
        input_id=input_id,
        task_id=task_id,
        payload_hash=payload_hash,
        failure_code=failure_code,
    )


def list_events(
    connection: sqlite3.Connection, limit: int
) -> list[EventSummary]:
    rows = connection.execute(
        "SELECT id, status, outcome, failure_code FROM inbound_events "
        "ORDER BY received_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()

    return [
        EventSummary(
            event_id=event_id,
            status=status,
            outcome=outcome,
            failure_code=failure_code,
        )
        for event_id, status, outcome, failure_code in rows
    ]


def insert_processing_event(
    connection: sqlite3.Connection,
    event_id: str,
    payload_hash: str,
    event_type: str,
    received_at: str,
) -> None:
    connection.execute(
        "INSERT INTO inbound_events "
        "(id, payload_hash, status, event_type, received_at, outcome) "
        "VALUES (?, ?, 'processing', ?, ?, 'candidate')",
        (event_id, payload_hash, event_type, received_at),
    )


def insert_accepted_event(
    connection: sqlite3.Connection, event: NewAcceptedEvent
) -> None:
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
) -> None:
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
) -> None:
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
        "WHERE status = 'processing'"
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
    row = connection.execute(
        "SELECT baseline_head, baseline_status, baseline_branch, "
        "baseline_index_manifest FROM admission_candidates WHERE id = ? "
        "AND status = 'captured'",
        (candidate_id,),
    ).fetchone()
    if row is None:
        return None

    head, status, branch, index_manifest = row

    return CapturedCandidate(
        baseline_head=head,
        baseline_status=status,
        baseline_branch=branch,
        baseline_index_manifest=index_manifest,
    )


def list_candidate_ids_by_event(
    connection: sqlite3.Connection, event_id: str
) -> list[CandidateRef]:
    rows = connection.execute(
        "SELECT id FROM admission_candidates WHERE event_id = ?",
        (event_id,),
    ).fetchall()

    return [
        CandidateRef(candidate_id=candidate_id) for (candidate_id,) in rows
    ]


def list_captured_candidate_ids(
    connection: sqlite3.Connection,
) -> list[CandidateRef]:
    rows = connection.execute(
        "SELECT id FROM admission_candidates WHERE status = 'captured'"
    ).fetchall()

    return [
        CandidateRef(candidate_id=candidate_id) for (candidate_id,) in rows
    ]


def mark_candidate_promoted(
    connection: sqlite3.Connection, candidate_id: str, input_id: str
) -> None:
    connection.execute(
        "UPDATE admission_candidates SET status = 'promoted', "
        "outcome = "
        "'admitted', input_id = ? WHERE id = ?",
        (input_id, candidate_id),
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
    rows = connection.execute(
        "SELECT path, status, sha256, size, is_binary, content "
        "FROM candidate_baseline_files WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchall()

    return [
        BaselineFileRow(
            path=path,
            status=status,
            sha256=sha256,
            size=size,
            is_binary=is_binary,
            content=content,
        )
        for path, status, sha256, size, is_binary, content in rows
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
    row = connection.execute(
        "SELECT admission_hash, outcome, event_id, reference_task_id "
        "FROM admission_no_input_decisions WHERE adapter = ? "
        "AND agent_session_id = ? AND native_input_id = ?",
        (adapter, agent_session_id, native_input_id),
    ).fetchone()
    if row is None:
        return None
    admission_hash, outcome, event_id, reference_task_id = row
    return NoInputDecisionRow(
        admission_hash=admission_hash,
        outcome=outcome,
        event_id=event_id,
        reference_task_id=reference_task_id,
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
