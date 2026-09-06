from __future__ import annotations

from pydantic import BaseModel


class InboundEvent(BaseModel):
    payload_hash: str
    status: str
    outcome: str
    input_id: str | None = None
    task_id: str | None = None
    failure_code: str | None = None


class InboundEventDetail(BaseModel):
    status: str
    outcome: str
    input_id: str | None = None
    task_id: str | None = None
    payload_hash: str
    failure_code: str | None = None


class EventSummary(BaseModel):
    event_id: str
    status: str
    outcome: str
    failure_code: str | None = None


class CapturedCandidate(BaseModel):
    baseline_head: bytes | str | None = None
    baseline_status: bytes | None = None
    baseline_branch: str | None = None
    baseline_index_manifest: bytes | None = None


class CandidateRef(BaseModel):
    candidate_id: str


class SessionTreeRef(BaseModel):
    session_id: str
    tree_id: str


class ActiveTaskRef(BaseModel):
    id: str
    session_id: str


class TaskOwnerRef(BaseModel):
    session_id: str
    tree_id: str
    status: str


class BaselineFileRow(BaseModel):
    path: str
    status: str
    sha256: str | None = None
    size: int | None = None
    is_binary: int | None = None
    content: bytes | None = None


class StoredInput(BaseModel):
    id: str
    task_id: str | None = None
    admission_hash: str


class TaskPageRow(BaseModel):
    id: str
    status: str
    started_at: str
    worktree: str
    project_id: str
    branch: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None


class TaskDetailRow(TaskPageRow):
    baseline_head: bytes | str | None = None
    baseline_status: bytes | None = None
    baseline_index_manifest: bytes | None = None


class TaskInputLink(BaseModel):
    task_id: str
    input_id: str


class InputRef(BaseModel):
    input_id: str


class NewAcceptedEvent(BaseModel):
    event_id: str
    payload_hash: str
    event_type: str
    received_at: str
    input_id: str
    task_id: str | None = None


class NewAdmissionCandidate(BaseModel):
    candidate_id: str
    session_id: str
    native_input_id: str
    baseline_head: bytes | str | None = None
    baseline_status: bytes | None = None
    baseline_branch: str | None = None
    baseline_index_manifest: bytes | None = None
    created_at: str
    admission_hash: str
    event_id: str


class NewTask(BaseModel):
    task_id: str
    session_id: str
    tree_id: str
    started_at: str
    baseline_head: bytes | str | None = None
    baseline_status: bytes | None = None
    baseline_branch: str | None = None
    baseline_index_manifest: bytes | None = None


class NewStoredInput(BaseModel):
    row_id: str
    session_id: str
    task_id: str
    input_id: str
    admission_hash: str


class NewNoInputDecision(BaseModel):
    adapter: str
    agent_session_id: str
    native_input_id: str
    admission_hash: str
    outcome: str
    event_id: str
    reference_task_id: str | None = None
    created_at: str


class NoInputDecisionRow(BaseModel):
    admission_hash: str
    outcome: str
    event_id: str
    reference_task_id: str | None = None
