from __future__ import annotations

from pydantic import BaseModel


class InboundEvent(BaseModel):
    payload_hash: str
    semantic_hash: str | None = None
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
    semantic_hash: str | None = None
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
    execution_id: str | None = None


class FinalizationEventRow(BaseModel):
    payload_hash: str
    semantic_hash: str | None = None
    status: str
    outcome: str
    input_id: str | None = None
    task_id: str | None = None
    failure_code: str | None = None


class FrozenTaskState(BaseModel):
    status: str
    snapshot_frozen_at: str | None = None


class GenerationRef(BaseModel):
    capture_generation: int


class RecoveryTaskRef(BaseModel):
    task_id: str
    snapshot_frozen_at: str | None = None


class FinalizationTask(BaseModel):
    id: str
    session_id: str
    tree_id: str
    status: str
    git_root: str
    project_id: str
    adapter: str
    adapter_version: str | None = None
    agent_session_id: str
    workspace_path: str | None = None
    baseline_head: str
    baseline_branch: str
    baseline_index_manifest: bytes
    capture_generation: int
    execution_id: str | None = None
    terminal_observed_at: str | None = None
    capture_not_after: str | None = None


class TaskFileChangeRow(BaseModel):
    path: str
    operation: str | None = None
    final_status: str
    final_sha256: str | None = None
    final_size: int | None = None
    final_is_binary: int | None = None
    final_content: bytes | None = None
    evidence_status: str
    evidence_reason: str | None = None
    patch: str | None = None
    baseline_mode: str | None = None
    baseline_gitlink_oid: str | None = None
    final_mode: str | None = None
    final_gitlink_oid: str | None = None


class BaselineFileRow(BaseModel):
    path: str
    status: str
    sha256: str | None = None
    size: int | None = None
    is_binary: int | None = None
    content: bytes | None = None
    mode: str | None = None
    gitlink_oid: str | None = None


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
    final_head: str | None = None
    final_branch: str | None = None
    final_status: bytes | None = None
    final_index_manifest: bytes | None = None
    snapshot_frozen_at: str | None = None
    task_diff: str | None = None
    evidence_completeness: str | None = None
    execution_id: str | None = None
    terminal_signal: str | None = None
    terminal_outcome: str | None = None
    compatibility_profile: str | None = None
    terminal_observed_at: str | None = None
    capture_not_after: str | None = None


class TaskInputLink(BaseModel):
    task_id: str
    input_id: str


class InputRef(BaseModel):
    input_id: str


class NewAcceptedEvent(BaseModel):
    event_id: str
    payload_hash: str
    semantic_hash: str | None = None
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
    execution_id: str
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
