"""Replay, hashes and reconciliation for admissions.

Locked helpers run inside coordinator-owned transactions. This module
never opens connections, begins transactions or captures baselines;
the coordinator owns those effects.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from crucible_core.core.errors import AdmissionError
from crucible_core.repositories import admissions_repository as admissions_repo
from crucible_core.repositories import tasks_repository as tasks_repo
from crucible_core.responses.admissions import reconciled_event_response
from crucible_core.schemas.admissions import EventRequest
from crucible_core.schemas.persistence import (
    InboundEvent,
    NewAcceptedEvent,
    StoredInput,
)
from crucible_core.utils.functions import canonical_json_sha256

from .decisions import check_execution_match, decide_no_input_outcome

SemanticKind = Literal["none", "admitted", "overlap", "steer"]


def payload_hash(event: EventRequest) -> str:
    payload = event.model_dump(mode="json", exclude_none=True)
    return canonical_json_sha256(payload)


def admission_hash(event: EventRequest) -> str:
    payload = event.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"event_id", "occurred_at", "execution_id"},
    )
    return canonical_json_sha256(payload)


def stored_matches(
    event: EventRequest,
    incoming_transport: str,
    row: InboundEvent,
) -> bool:
    if row.payload_hash == incoming_transport:
        return True
    incoming_semantic = payload_hash(event)
    if row.semantic_hash is not None:
        return row.semantic_hash == incoming_semantic
    return row.payload_hash == incoming_semantic


def reconcile_existing(
    event_id: str,
    payload_hash: str,
    row: InboundEvent | None,
    event: EventRequest,
) -> dict[str, object]:
    if row is None:
        raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
    if not stored_matches(event, payload_hash, row):
        raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
    if row.failure_code == "STEER_WITHOUT_ACTIVE_TASK":
        raise AdmissionError("STEER_WITHOUT_ACTIVE_TASK", 409)
    return reconciled_event_response(event_id, row).model_dump()


def classify_semantic_locked(
    connection: sqlite3.Connection,
    event: EventRequest,
) -> tuple[SemanticKind, StoredInput | None]:
    """Classify an already observed input without writing.

    Returns the replay kind plus the stored input when the event was
    admitted before. Raises on hash or execution mismatches exactly
    like the coordinator historically did inline.
    """
    stored = tasks_repo.find_input_by_adapter_session(
        connection,
        event.adapter,
        event.agent_session_id,
        event.input_id,
    )
    if stored is not None:
        if stored.admission_hash != admission_hash(event):
            raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
        stored_owner = tasks_repo.get_task_owner(connection, stored.task_id)
        check_execution_match(
            stored_owner.execution_id if stored_owner else None,
            event.execution_id,
        )
        return ("admitted", stored)
    decision = admissions_repo.find_no_input_decision(
        connection,
        event.adapter,
        event.agent_session_id,
        event.input_id,
    )
    if decision is None:
        return ("none", None)
    if decision.admission_hash != admission_hash(event):
        raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
    if decide_no_input_outcome(decision.outcome) == "released_overlap":
        return ("overlap", None)
    return ("steer", None)


def record_admitted_input_locked(
    connection: sqlite3.Connection,
    *,
    event: EventRequest,
    event_id: str,
    transport_hash: str,
    stored: StoredInput,
    takeover: bool,
    now: str,
) -> None:
    if takeover:
        admissions_repo.mark_event_accepted(
            connection, event_id, stored.id, stored.task_id
        )
        return
    admissions_repo.insert_accepted_event(
        connection,
        NewAcceptedEvent(
            event_id=event_id,
            payload_hash=transport_hash,
            semantic_hash=payload_hash(event),
            event_type=event.event_type,
            received_at=now,
            input_id=stored.id,
            task_id=stored.task_id,
        ),
    )


def record_overlap_locked(
    connection: sqlite3.Connection,
    *,
    event: EventRequest,
    event_id: str,
    transport_hash: str,
    takeover: bool,
    now: str,
) -> None:
    if takeover:
        admissions_repo.mark_event_accepted_overlap(connection, event_id)
        return
    admissions_repo.insert_accepted_overlap_event(
        connection,
        event_id,
        transport_hash,
        event.event_type,
        now,
        payload_hash(event),
    )


def record_steer_locked(
    connection: sqlite3.Connection,
    *,
    event: EventRequest,
    event_id: str,
    transport_hash: str,
    takeover: bool,
    now: str,
) -> None:
    if takeover:
        admissions_repo.mark_event_rejected(
            connection, event_id, "STEER_WITHOUT_ACTIVE_TASK"
        )
        return
    admissions_repo.insert_rejected_event(
        connection,
        event_id,
        transport_hash,
        event.event_type,
        now,
        "STEER_WITHOUT_ACTIVE_TASK",
        payload_hash(event),
    )
