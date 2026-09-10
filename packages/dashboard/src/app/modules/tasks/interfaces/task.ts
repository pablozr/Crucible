/**
 * Core `/v1` API contracts mirrored from
 * `core/src/crucible_core/schemas/admissions.py` and `envelope.py`.
 * Nullability matches Core schemas exactly: absent evidence is `null`,
 * never fabricated.
 */

export interface ResponseEnvelope<T> {
  status: 'ok' | 'error';
  message: string;
  data: T;
}

export interface TaskSummary {
  id: string;
  status: string;
  started_at: string;
  worktree: string;
  project_id: string;
  branch: string | null;
  failure_code: string | null;
  failure_message: string | null;
}

export interface BaselineFile {
  path: string;
  status: string;
  sha256: string | null;
  size: number | null;
  is_binary: boolean | null;
  content: string | null;
  mode: string | null;
  gitlink_oid: string | null;
}

export interface TaskFileChange {
  path: string;
  operation: string | null;
  final_status: string;
  final_sha256: string | null;
  final_size: number | null;
  final_is_binary: boolean | null;
  evidence_status: string;
  evidence_reason: string | null;
  patch: string | null;
  baseline_mode: string | null;
  baseline_gitlink_oid: string | null;
  final_mode: string | null;
  final_gitlink_oid: string | null;
}

export interface TaskDetail extends TaskSummary {
  baseline_head: string | null;
  baseline_status: string | null;
  baseline_index_manifest: string | null;
  baseline_index_sha256: string | null;
  input_ids: string[];
  baseline_files: BaselineFile[];
  final_head: string | null;
  final_branch: string | null;
  final_status: string | null;
  final_index_manifest: string | null;
  final_index_sha256: string | null;
  snapshot_frozen_at: string | null;
  task_diff: string | null;
  evidence_completeness: string | null;
  execution_id: string | null;
  terminal_signal: string | null;
  terminal_outcome: string | null;
  compatibility_profile: string | null;
  terminal_observed_at: string | null;
  capture_not_after: string | null;
  file_changes: TaskFileChange[];
}

export interface TasksData {
  tasks: TaskSummary[];
  next_cursor: string | null;
}

export interface TaskData {
  task: TaskDetail;
}
