import { createHash, randomUUID } from "node:crypto";
import { mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

import { ADAPTER, ADAPTER_VERSION } from "../opencode-v1/contracts.js";
import { loadSqliteDriver, type SqliteDatabase } from "./sqlite-driver.js";
import {
  COMPLETION_UNCONFIRMED,
  canonicalJson,
  getEvent,
  postCanonicalEvent,
} from "./core-client.js";
import type {
  Admission,
  AdmittedTerminalInput,
  CandidateReservationInput,
  CanonicalCandidate,
  CoreConnectionOptions,
  FetchImpl,
  OutboxDelivery,
  OutboxDeliveryOptions,
  TerminalAbortReason,
  TerminalEnvelopeInput,
  TerminalOutboxPolicy,
  TerminalOutboxTimer,
} from "./contracts.js";

export type {
  AdmittedTerminalInput,
  CandidateReservationInput,
  OutboxDelivery,
  OutboxDeliveryOptions,
  TerminalAbortReason,
  TerminalEnvelopeInput,
  TerminalOutboxPolicy,
  TerminalOutboxTimer,
} from "./contracts.js";

// P14/E10 production candidates (2026-09-07): the reservation exceeds the
// worst measured envelope by 3.45x, and the authorization window covers the
// worst measured terminal delivery p99 by 2.86x.
export const TERMINAL_RESERVATION_BYTES = 16_384;
export const OUTBOX_MAX_BYTES = 201_326_592;
export const TERMINAL_MAX_AUTHORIZATION_WINDOW_MS = 2_000;

const defaultTimer: TerminalOutboxTimer = {
  schedule: (callback, delayMs) => setTimeout(callback, delayMs),
  cancel: (handle) => clearTimeout(handle as NodeJS.Timeout),
};

// Fills production defaults around an explicit or injected test policy;
// terminal behavior itself stays opt-in at the dispatch layer.
export function createTerminalOutboxPolicy(
  overrides: Partial<TerminalOutboxPolicy> = {},
): TerminalOutboxPolicy {
  return {
    maxBytes: OUTBOX_MAX_BYTES,
    reservationBytes: TERMINAL_RESERVATION_BYTES,
    authorizationWindowMs: TERMINAL_MAX_AUTHORIZATION_WINDOW_MS,
    leaseMs: 5_000,
    backoffBaseMs: 500,
    backoffMaxMs: 30_000,
    clock: () => Date.now(),
    jitter: (maximumDelayMs) => Math.random() * maximumDelayMs,
    timer: defaultTimer,
    busyTimeoutMs: 5_000,
    ...overrides,
  };
}

type OutboxRow = {
  id: string;
  reserved_bytes: number;
  event_id: string | null;
  task_id: string | null;
  input_id: string | null;
  envelope: string | null;
  payload_hash: string | null;
  abort_event_id: string | null;
  abort_envelope: string | null;
  abort_payload_hash: string | null;
  abort_reason: string | null;
  attempts: number;
};

const TERMINAL_ABORT_REASONS = [
  "DISPATCH_FAILED",
  "TERMINAL_OBSERVER_FAILED",
  "TERMINAL_SIGNAL_MISMATCH",
] as const satisfies readonly TerminalAbortReason[];

// A persisted abort envelope/event/hash is immutable: a later terminal
// observation must never clear or replace it with a completion.
export const TERMINAL_ABORT_PERSISTED = "TERMINAL_ABORT_PERSISTED";

type CandidateRow = {
  id: string;
  candidate_event_id: string;
  candidate_payload_hash: string;
  attempts: number;
  last_diagnostic: string | null;
};

function requirePolicy(policy: TerminalOutboxPolicy): void {
  const positive = [
    policy.maxBytes,
    policy.reservationBytes,
    policy.authorizationWindowMs,
    policy.leaseMs,
    policy.backoffBaseMs,
    policy.backoffMaxMs,
    policy.busyTimeoutMs,
  ];
  if (positive.some((value) => !Number.isFinite(value) || value <= 0)) {
    throw new Error("INVALID_TERMINAL_OUTBOX_POLICY");
  }
  if (policy.reservationBytes > policy.maxBytes) {
    throw new Error("INVALID_TERMINAL_OUTBOX_POLICY");
  }
}

export function resolveCrucibleDataDir(environment = process.env): string {
  if (environment["CRUCIBLE_DATA_DIR"]) {
    return resolve(environment["CRUCIBLE_DATA_DIR"]);
  }
  if (process.platform === "win32") {
    return join(environment["LOCALAPPDATA"] || join(homedir(), "AppData", "Local"), "Crucible");
  }
  if (process.platform === "darwin") {
    return join(homedir(), "Library", "Application Support", "Crucible");
  }
  return join(environment["XDG_DATA_HOME"] || join(homedir(), ".local", "share"), "Crucible");
}

function responseEvent(body: unknown): Record<string, unknown> | undefined {
  if (!body || typeof body !== "object" || Array.isArray(body)) return undefined;
  const data = (body as Record<string, unknown>)["data"];
  if (!data || typeof data !== "object" || Array.isArray(data)) return undefined;
  const event = (data as Record<string, unknown>)["event"];
  return event && typeof event === "object" && !Array.isArray(event)
    ? (event as Record<string, unknown>)
    : undefined;
}

export class TerminalOutbox {
  readonly databasePath: string;
  readonly driverName: string;
  private readonly database: SqliteDatabase;

  private constructor(
    private readonly policy: TerminalOutboxPolicy,
    database: SqliteDatabase,
    databasePath: string,
    driverName: string,
  ) {
    this.database = database;
    this.databasePath = databasePath;
    this.driverName = driverName;
  }

  get reservationBytes(): number {
    return this.policy.reservationBytes;
  }

  static async open(policy: TerminalOutboxPolicy): Promise<TerminalOutbox> {
    requirePolicy(policy);
    const driver = await loadSqliteDriver();
    const dataDir = resolve(policy.dataDir ?? resolveCrucibleDataDir());
    mkdirSync(dataDir, { recursive: true });
    const databasePath = join(dataDir, "adapter-terminal-outbox.db");
    const database = driver.open(databasePath);
    try {
      const outbox = new TerminalOutbox(policy, database, databasePath, driver.name);
      outbox.initializeSchema();
      return outbox;
    } catch (error) {
      try {
        database.close();
      } catch {
        // The original failure stays authoritative.
      }
      throw error;
    }
  }

  private initializeSchema(): void {
    this.database.exec(
      `PRAGMA busy_timeout=${Math.trunc(this.policy.busyTimeoutMs)}; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;`,
    );
    this.database.exec(`
      CREATE TABLE IF NOT EXISTS terminal_outbox (
        id TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        reserved_bytes INTEGER NOT NULL,
        accounted_bytes INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        agent_session_id TEXT NOT NULL,
        execution_id TEXT NOT NULL,
        compatibility_profile TEXT NOT NULL,
        project_id TEXT,
        git_root TEXT,
        workspace_path TEXT,
        candidate_event_id TEXT UNIQUE,
        candidate_envelope TEXT,
        candidate_payload_hash TEXT,
        terminal_observed_at TEXT,
        capture_not_after TEXT,
        task_id TEXT,
        input_id TEXT,
        native_input_id TEXT,
        event_id TEXT UNIQUE,
        envelope TEXT,
        payload_hash TEXT,
        abort_event_id TEXT,
        abort_envelope TEXT,
        abort_payload_hash TEXT,
        abort_reason TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at INTEGER,
        lease_until INTEGER,
        last_diagnostic TEXT
      ) STRICT;
      CREATE INDEX IF NOT EXISTS terminal_outbox_due
        ON terminal_outbox(state, next_attempt_at, lease_until);
      CREATE TABLE IF NOT EXISTS terminal_outbox_capacity (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        used_bytes INTEGER NOT NULL
      ) STRICT;
    `);
    const columns = this.database.prepare("PRAGMA table_info(terminal_outbox)").all() as Array<{ name: string }>;
    if (!columns.some((column) => column.name === "terminal_observed_at")) {
      this.database.exec("ALTER TABLE terminal_outbox ADD COLUMN terminal_observed_at TEXT");
    }
    for (const name of ["project_id", "git_root", "workspace_path", "candidate_event_id", "candidate_envelope", "candidate_payload_hash", "native_input_id", "capture_not_after", "abort_event_id", "abort_envelope", "abort_payload_hash", "abort_reason"]) {
      if (!columns.some((column) => column.name === name)) {
        this.database.exec(`ALTER TABLE terminal_outbox ADD COLUMN ${name} TEXT`);
      }
    }
    this.database.exec(
      "CREATE UNIQUE INDEX IF NOT EXISTS terminal_outbox_abort_event ON terminal_outbox(abort_event_id)",
    );
    this.initializeCapacityCounter();
  }

  // One-time initialization: a legacy database without a counter row adopts
  // its unresolved reservation sum; fresh databases start at zero. Runs under
  // BEGIN IMMEDIATE so concurrent openers cannot double-insert or compute the
  // sum from divergent states.
  private initializeCapacityCounter(): void {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const counter = this.database
        .prepare("SELECT used_bytes FROM terminal_outbox_capacity WHERE id = 1")
        .get() as { used_bytes: number } | undefined;
      if (!counter) {
        const legacy = this.database
          .prepare("SELECT COALESCE(SUM(reserved_bytes), 0) AS used FROM terminal_outbox WHERE state != 'terminal'")
          .get() as { used: number };
        this.database
          .prepare("INSERT INTO terminal_outbox_capacity (id, used_bytes) VALUES (1, ?)")
          .run(legacy.used);
      }
      this.database.exec("COMMIT");
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  close(): void {
    this.database.close();
  }

  reserve(agentSessionId: string, executionId: string, profile: string): string | null {
    const id = randomUUID();
    const now = this.policy.clock();
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const used = this.database
        .prepare("SELECT used_bytes FROM terminal_outbox_capacity WHERE id = 1")
        .get() as { used_bytes: number };
      if (used.used_bytes + this.policy.reservationBytes > this.policy.maxBytes) {
        this.database.exec("ROLLBACK");
        return null;
      }
      this.database.prepare(`
        INSERT INTO terminal_outbox
          (id, state, reserved_bytes, accounted_bytes, created_at,
           agent_session_id, execution_id, compatibility_profile)
        VALUES (?, 'reserved', ?, ?, ?, ?, ?, ?)
      `).run(id, this.policy.reservationBytes, this.policy.reservationBytes, now,
        agentSessionId, executionId, profile);
      this.database
        .prepare("UPDATE terminal_outbox_capacity SET used_bytes = used_bytes + ? WHERE id = 1")
        .run(this.policy.reservationBytes);
      this.database.exec("COMMIT");
      return id;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  reserveCandidate(input: CandidateReservationInput): string | null {
    const id = randomUUID();
    const now = this.policy.clock();
    const candidateBytes = Buffer.byteLength(input.candidate.envelope);
    if (candidateBytes > this.policy.reservationBytes) return null;

    this.database.exec("BEGIN IMMEDIATE");
    try {
      const used = this.database
        .prepare("SELECT used_bytes FROM terminal_outbox_capacity WHERE id = 1")
        .get() as { used_bytes: number };
      if (used.used_bytes + this.policy.reservationBytes > this.policy.maxBytes) {
        this.database.exec("ROLLBACK");
        return null;
      }
      this.database.prepare(`
        INSERT INTO terminal_outbox
          (id, state, reserved_bytes, accounted_bytes, created_at,
           agent_session_id, execution_id, compatibility_profile, project_id,
           git_root, workspace_path, candidate_event_id, candidate_envelope,
           candidate_payload_hash, next_attempt_at)
        VALUES (?, 'candidate_in_flight', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `).run(
        id, this.policy.reservationBytes, this.policy.reservationBytes, now,
        input.agentSessionId, input.executionId, input.compatibilityProfile,
        input.projectId, input.gitRoot, input.workspacePath,
        input.candidate.eventId, input.candidate.envelope,
        input.candidate.payloadHash, now + input.reconciliationDelayMs,
      );
      this.database
        .prepare("UPDATE terminal_outbox_capacity SET used_bytes = used_bytes + ? WHERE id = 1")
        .run(this.policy.reservationBytes);
      this.database.exec("COMMIT");
      return id;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  cancelReservation(id: string): void {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const row = this.database
        .prepare("SELECT reserved_bytes FROM terminal_outbox WHERE id = ? AND state = 'reserved'")
        .get(id) as { reserved_bytes: number } | undefined;
      if (row) {
        const deleted = this.database
          .prepare("DELETE FROM terminal_outbox WHERE id = ? AND state = 'reserved'")
          .run(id);
        if (deleted.changes === 1) {
          this.database
            .prepare("UPDATE terminal_outbox_capacity SET used_bytes = MAX(used_bytes - ?, 0) WHERE id = 1")
            .run(row.reserved_bytes);
        }
      }
      this.database.exec("COMMIT");
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  bindAdmission(id: string, taskId: string, inputId: string): void {
    const row = this.database
      .prepare("SELECT candidate_envelope FROM terminal_outbox WHERE id = ?")
      .get(id) as { candidate_envelope: string | null } | undefined;
    if (!row) throw new Error("OUTBOX_RESERVATION_NOT_FOUND");

    let nativeInputId = inputId;
    if (row.candidate_envelope) {
      try {
        const candidate = JSON.parse(row.candidate_envelope) as { input_id?: unknown };
        if (typeof candidate.input_id === "string" && candidate.input_id) {
          nativeInputId = candidate.input_id;
        }
      } catch {
        // A reserved row falls back to the Core-correlated input row identity.
      }
    }

    const result = this.database.prepare(`
      UPDATE terminal_outbox SET state = 'admitted', task_id = ?, input_id = ?, native_input_id = ?
      WHERE id = ? AND state IN ('reserved', 'candidate_in_flight')
    `).run(taskId, inputId, nativeInputId, id);
    if (result.changes !== 1) throw new Error("OUTBOX_RESERVATION_NOT_FOUND");
  }

  reconcileCandidateResponse(id: string, admission: Admission): void {
    if (admission.tracked && admission.taskId && admission.inputId) {
      this.bindAdmission(id, admission.taskId, admission.inputId);
      return;
    }
    if (!admission.tracked && admission.outcome !== "tracking_skipped") {
      this.releaseCandidate(id, admission.diagnostic);
    }
  }

  markAborted(id: string, reason: TerminalAbortReason): void {
    if (!TERMINAL_ABORT_REASONS.includes(reason)) {
      throw new Error("INVALID_ABORT_REASON");
    }
    const row = this.database.prepare(`
      SELECT task_id, input_id, native_input_id, agent_session_id, execution_id,
             project_id, git_root, workspace_path,
             abort_event_id, abort_envelope, abort_payload_hash, abort_reason
      FROM terminal_outbox WHERE id = ? AND state = 'admitted'
    `).get(id) as {
      task_id: string | null;
      input_id: string | null;
      native_input_id: string | null;
      agent_session_id: string;
      execution_id: string;
      project_id: string | null;
      git_root: string | null;
      workspace_path: string | null;
      abort_event_id: string | null;
      abort_envelope: string | null;
      abort_payload_hash: string | null;
      abort_reason: string | null;
    } | undefined;
    if (!row) throw new Error("OUTBOX_ADMISSION_NOT_FOUND");

    const now = this.policy.clock();

    if (row.abort_event_id && row.abort_envelope && row.abort_payload_hash) {
      const rearm = this.database.prepare(`
        UPDATE terminal_outbox SET state = 'aborted', next_attempt_at = ?, lease_until = NULL
        WHERE id = ? AND state = 'admitted'
      `).run(now, id);
      if (rearm.changes !== 1) throw new Error("OUTBOX_ADMISSION_NOT_FOUND");
      return;
    }

    const nativeInputId = row.native_input_id ?? row.input_id;
    if (!row.task_id || !nativeInputId || !row.project_id || !row.git_root || !row.workspace_path) {
      throw new Error("OUTBOX_ADMISSION_IDENTITY_INCOMPLETE");
    }
    const eventId = randomUUID();
    const envelope = canonicalJson({
      adapter: ADAPTER,
      adapter_version: ADAPTER_VERSION,
      agent_session_id: row.agent_session_id,
      event_id: eventId,
      event_type: "task_finalization_aborted",
      execution_id: row.execution_id,
      git_root: row.git_root,
      input_id: nativeInputId,
      occurred_at: new Date(now).toISOString(),
      payload: {
        abort_reason: reason,
        task_id: row.task_id,
      },
      payload_version: 1,
      project_id: row.project_id,
      workspace_path: row.workspace_path,
    });
    const payloadHash = createHash("sha256").update(envelope).digest("hex");
    const result = this.database.prepare(`
      UPDATE terminal_outbox SET state = 'aborted', abort_event_id = ?, abort_envelope = ?,
        abort_payload_hash = ?, abort_reason = ?, last_diagnostic = ?, next_attempt_at = ?,
        lease_until = NULL
      WHERE id = ? AND state = 'admitted' AND abort_event_id IS NULL
    `).run(eventId, envelope, payloadHash, reason, reason, now, id);
    if (result.changes !== 1) throw new Error("OUTBOX_ADMISSION_NOT_FOUND");
  }

  private releaseCandidate(id: string, diagnostic: string): void {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const row = this.database
        .prepare("SELECT reserved_bytes FROM terminal_outbox WHERE id = ? AND state = 'candidate_in_flight'")
        .get(id) as { reserved_bytes: number } | undefined;
      if (row) {
        this.database.prepare(`
          UPDATE terminal_outbox SET state = 'terminal', accounted_bytes = 0,
            next_attempt_at = NULL, lease_until = NULL, last_diagnostic = ?
          WHERE id = ? AND state = 'candidate_in_flight'
        `).run(diagnostic, id);
        this.database
          .prepare("UPDATE terminal_outbox_capacity SET used_bytes = MAX(used_bytes - ?, 0) WHERE id = 1")
          .run(row.reserved_bytes);
      }
      this.database.exec("COMMIT");
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  private async reconcileCandidate(
    options: CoreConnectionOptions,
    row: CandidateRow,
  ): Promise<void> {
    try {
      const response = await getEvent(row.candidate_event_id, options);
      if (response.status === 404) {
        const data = responseEvent(response.body);
        const errorData = response.body && typeof response.body === "object"
          ? (response.body as Record<string, unknown>)["data"]
          : undefined;
        if (!data && errorData && typeof errorData === "object" &&
            (errorData as Record<string, unknown>)["code"] === "EVENT_NOT_FOUND") {
          if (row.last_diagnostic === "EVENT_NOT_FOUND") {
            this.releaseCandidate(row.id, "EVENT_NOT_FOUND");
          } else {
            this.retryCandidate(row, "EVENT_NOT_FOUND");
          }
          return;
        }
        this.retryCandidate(row, "CANDIDATE_RECONCILIATION_UNCONFIRMED");
        return;
      }
      const event = responseEvent(response.body);
      if (!event || event["event_id"] !== row.candidate_event_id ||
          event["payload_hash"] !== row.candidate_payload_hash) {
        this.retryCandidate(row, "CANDIDATE_RECONCILIATION_MISMATCH");
        return;
      }
      if (event["status"] === "accepted" && event["outcome"] === "admitted" &&
          event["dispatch_authorized"] === true && typeof event["task_id"] === "string" &&
          typeof event["input_id"] === "string") {
        this.bindAdmission(row.id, event["task_id"], event["input_id"]);
      } else if (event["status"] === "rejected" && event["dispatch_authorized"] === false) {
        this.releaseCandidate(row.id, String(event["failure_code"] ?? event["outcome"] ?? "CANDIDATE_REJECTED"));
      } else {
        this.retryCandidate(row, "CANDIDATE_RECONCILIATION_PENDING");
      }
    } catch {
      this.retryCandidate(row, "CANDIDATE_RECONCILIATION_UNCONFIRMED");
    }
  }

  private retryCandidate(row: CandidateRow, diagnostic: string): void {
    const exponent = Math.min(row.attempts, 30);
    const ceiling = Math.min(
      this.policy.backoffMaxMs,
      this.policy.backoffBaseMs * 2 ** exponent,
    );
    const delay = Math.max(
      this.policy.backoffBaseMs,
      Math.min(ceiling, this.policy.jitter(ceiling)),
    );
    this.database.prepare(`
      UPDATE terminal_outbox SET attempts = attempts + 1, next_attempt_at = ?,
        last_diagnostic = ? WHERE id = ? AND state = 'candidate_in_flight'
    `).run(this.policy.clock() + delay, diagnostic, row.id);
  }

  storeCompletionForAdmission(input: AdmittedTerminalInput): {
    obligationId: string;
    eventId: string;
    bytes: number;
    payloadHash: string;
  } {
    const row = this.database.prepare(`
      SELECT id, input_id, native_input_id, task_id, project_id, git_root, workspace_path,
             abort_event_id, abort_envelope, abort_payload_hash
      FROM terminal_outbox
      WHERE agent_session_id = ? AND execution_id = ?
        AND (state = 'admitted' OR abort_envelope IS NOT NULL)
      ORDER BY created_at DESC LIMIT 1
    `).get(input.agentSessionId, input.executionId) as {
      id: string;
      input_id: string;
      native_input_id: string | null;
      task_id: string;
      project_id: string;
      git_root: string;
      workspace_path: string;
      abort_event_id: string | null;
      abort_envelope: string | null;
      abort_payload_hash: string | null;
    } | undefined;
    if (!row) throw new Error("OUTBOX_ADMISSION_NOT_FOUND");
    if (row.abort_event_id && row.abort_envelope && row.abort_payload_hash) {
      throw new Error(TERMINAL_ABORT_PERSISTED);
    }
    const nativeInputId = row.native_input_id ?? row.input_id;
    if (!row.input_id || !nativeInputId || !row.task_id || !row.project_id || !row.git_root || !row.workspace_path) {
      throw new Error("OUTBOX_ADMISSION_IDENTITY_INCOMPLETE");
    }

    return {
      obligationId: row.id,
      ...this.storeCompletion(row.id, {
        ...input,
        inputId: nativeInputId,
        taskId: row.task_id,
        projectId: row.project_id,
        gitRoot: row.git_root,
        workspacePath: row.workspace_path,
      }),
    };
  }

  markAdmissionAborted(agentSessionId: string, executionId: string, reason: TerminalAbortReason): string {
    const row = this.database.prepare(`
      SELECT id FROM terminal_outbox WHERE agent_session_id = ? AND execution_id = ?
        AND state = 'admitted' ORDER BY created_at DESC LIMIT 1
    `).get(agentSessionId, executionId) as { id: string } | undefined;
    if (!row) throw new Error("OUTBOX_ADMISSION_NOT_FOUND");
    this.markAborted(row.id, reason);
    return row.id;
  }

  storeCompletion(id: string, input: TerminalEnvelopeInput): { eventId: string; bytes: number; payloadHash: string } {
    const row = this.database.prepare(
      "SELECT state, native_input_id, input_id, abort_event_id, abort_envelope, abort_payload_hash FROM terminal_outbox WHERE id = ?",
    ).get(id) as {
      state: string;
      native_input_id: string | null;
      input_id: string | null;
      abort_event_id: string | null;
      abort_envelope: string | null;
      abort_payload_hash: string | null;
    } | undefined;
    if (!row) throw new Error("OUTBOX_ADMISSION_NOT_FOUND");
    if (row.abort_event_id && row.abort_envelope && row.abort_payload_hash) {
      throw new Error(TERMINAL_ABORT_PERSISTED);
    }
    if (row.state !== "admitted") throw new Error("OUTBOX_ADMISSION_NOT_FOUND");
    const nativeInputId = row.native_input_id ?? row.input_id;
    if (!nativeInputId || nativeInputId !== input.inputId) {
      throw new Error("OUTBOX_ADMISSION_IDENTITY_MISMATCH");
    }
    const observedAt = Date.parse(input.terminalObservedAt);
    if (!Number.isFinite(observedAt)) {
      throw new Error("INVALID_TERMINAL_OBSERVED_AT");
    }
    const captureNotAfter = new Date(
      observedAt + this.policy.authorizationWindowMs,
    ).toISOString();
    const envelope = canonicalJson({
      adapter: input.adapter,
      adapter_version: input.adapterVersion,
      agent_session_id: input.agentSessionId,
      event_id: input.eventId,
      event_type: "task_completed",
      execution_id: input.executionId,
      git_root: input.gitRoot,
      input_id: input.inputId,
      occurred_at: input.occurredAt,
      payload: {
        capture_not_after: captureNotAfter,
        compatibility_profile: input.compatibilityProfile,
        task_id: input.taskId,
        terminal_observed_at: input.terminalObservedAt,
        terminal_outcome: input.terminalOutcome,
        terminal_signal: input.terminalSignal,
      },
      payload_version: 1,
      project_id: input.projectId,
      workspace_path: input.workspacePath,
    });
    const bytes = Buffer.byteLength(envelope);
    if (bytes > this.policy.reservationBytes) {
      throw new Error("TERMINAL_ENVELOPE_EXCEEDS_RESERVATION");
    }
    const payloadHash = createHash("sha256").update(envelope).digest("hex");
    // The fixed reservation charge persists through every unresolved state,
    // including 'ready': completion envelopes physically retain both the
    // candidate and terminal payload, so charging the terminal envelope alone
    // undercounts (P14). Only the terminal acknowledgement releases bytes.
    const result = this.database.prepare(`
      UPDATE terminal_outbox SET state = 'ready', event_id = ?, envelope = ?,
        payload_hash = ?, accounted_bytes = reserved_bytes, terminal_observed_at = ?,
        capture_not_after = ?, next_attempt_at = ?, last_diagnostic = NULL
      WHERE id = ? AND state = 'admitted' AND task_id = ?
        AND COALESCE(native_input_id, input_id) = ?
    `).run(
      input.eventId,
      envelope,
      payloadHash,
      input.terminalObservedAt,
      captureNotAfter,
      this.policy.clock(),
      id,
      input.taskId,
      input.inputId,
    );
    if (result.changes !== 1) throw new Error("OUTBOX_ADMISSION_NOT_FOUND");
    return { eventId: input.eventId, bytes, payloadHash };
  }

  private terminalResponse(
    row: OutboxRow,
    body: unknown,
    requirePayloadHash: boolean,
  ): OutboxDelivery | undefined {
    const abortMode = row.abort_envelope !== null && row.abort_envelope !== undefined;
    const expectedEventId = abortMode ? row.abort_event_id : row.event_id;
    const expectedHash = abortMode ? row.abort_payload_hash : row.payload_hash;
    const event = responseEvent(body);
    if (!event || event["event_id"] !== expectedEventId || event["task_id"] !== row.task_id ||
        event["input_id"] !== row.input_id || event["dispatch_authorized"] !== false ||
        (requirePayloadHash && event["payload_hash"] !== expectedHash)) {
      return undefined;
    }
    if (abortMode) {
      if (event["status"] !== "rejected" || event["outcome"] !== "rejected") return undefined;
      return {
        eventId: expectedEventId!,
        terminal: true,
        accepted: false,
        diagnostic: row.abort_reason ?? "ABORT_ACKNOWLEDGED",
      };
    }
    if (event["status"] !== "accepted" && event["status"] !== "rejected") return undefined;
    if (
      (event["status"] === "accepted" && event["outcome"] !== "completed") ||
      (event["status"] === "rejected" && event["outcome"] !== "rejected")
    ) {
      return undefined;
    }
    return {
      eventId: row.event_id!,
      terminal: true,
      accepted: event["status"] === "accepted",
      diagnostic: event["status"] === "rejected"
        ? String(event["failure_code"] ?? "COMPLETION_REJECTED")
        : undefined,
    };
  }

  private finish(row: OutboxRow, delivery: OutboxDelivery): OutboxDelivery {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const result = this.database.prepare(`
        UPDATE terminal_outbox SET state = 'terminal', accounted_bytes = 0,
          lease_until = NULL, next_attempt_at = NULL, last_diagnostic = ?
        WHERE id = ? AND state = 'leased'
      `).run(delivery.diagnostic ?? null, row.id);
      // The state guard makes the release exactly-once: a racing process whose
      // acknowledgement no longer transitions the row decrements nothing.
      if (result.changes === 1) {
        this.database
          .prepare("UPDATE terminal_outbox_capacity SET used_bytes = MAX(used_bytes - ?, 0) WHERE id = 1")
          .run(row.reserved_bytes);
      }
      this.database.exec("COMMIT");
      return delivery;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  private retry(row: OutboxRow, diagnostic: string): OutboxDelivery {
    const exponent = Math.min(row.attempts, 30);
    const ceiling = Math.min(
      this.policy.backoffMaxMs,
      this.policy.backoffBaseMs * 2 ** exponent,
    );
    const delay = Math.max(
      this.policy.backoffBaseMs,
      Math.min(ceiling, this.policy.jitter(ceiling)),
    );
    this.database.prepare(`
      UPDATE terminal_outbox SET state = 'ready', attempts = attempts + 1,
        next_attempt_at = ?, lease_until = NULL, last_diagnostic = ?
      WHERE id = ? AND state = 'leased'
    `).run(this.policy.clock() + delay, diagnostic, row.id);
    return { eventId: row.event_id!, terminal: false, accepted: false, diagnostic };
  }

  private lease(id?: string): OutboxRow | undefined {
    const now = this.policy.clock();
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const row = this.database.prepare(id ? `
        SELECT id, reserved_bytes, event_id, task_id, input_id, envelope, payload_hash,
               abort_event_id, abort_envelope, abort_payload_hash, abort_reason, attempts
        FROM terminal_outbox WHERE id = ? AND state IN ('ready', 'aborted', 'leased')
          AND (state IN ('ready', 'aborted') OR lease_until <= ?)
      ` : `
        SELECT id, reserved_bytes, event_id, task_id, input_id, envelope, payload_hash,
               abort_event_id, abort_envelope, abort_payload_hash, abort_reason, attempts
        FROM terminal_outbox WHERE state IN ('ready', 'aborted', 'leased')
          AND (state IN ('ready', 'aborted') AND next_attempt_at <= ?
            OR state = 'leased' AND lease_until <= ?)
        ORDER BY created_at LIMIT 1
      `).get(...(id ? [id, now] : [now, now])) as OutboxRow | undefined;
      if (!row) {
        this.database.exec("ROLLBACK");
        return undefined;
      }
      this.database.prepare("UPDATE terminal_outbox SET state = 'leased', lease_until = ? WHERE id = ?")
        .run(now + this.policy.leaseMs, row.id);
      this.database.exec("COMMIT");
      return row;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  async deliver(options: Omit<OutboxDeliveryOptions, "policy">, id?: string): Promise<OutboxDelivery | undefined> {
    const row = this.lease(id);
    if (!row) return undefined;
    const abortMode = row.abort_envelope !== null && row.abort_envelope !== undefined;
    const eventId = abortMode ? row.abort_event_id : row.event_id;
    const envelope = abortMode ? row.abort_envelope : row.envelope;
    if (!eventId || !envelope) return undefined;
    let postBody: unknown;
    try {
      postBody = await postCanonicalEvent(envelope, options);
      const direct = this.terminalResponse(row, postBody, false);
      if (direct) return this.finish(row, direct);
    } catch {
      // A lost response can still represent a committed Core event; reconcile below.
    }

    try {
      const detail = await getEvent(eventId, options);
      const reconciled = detail.status === 200
        ? this.terminalResponse(row, detail.body, true)
        : undefined;
      if (reconciled) return this.finish(row, reconciled);
    } catch {
      // Reachable not-found and transport failures both remain retryable.
    }
    return this.retry(row, COMPLETION_UNCONFIRMED);
  }

  async deliverDue(options: Omit<OutboxDeliveryOptions, "policy">): Promise<void> {
    const now = this.policy.clock();
    const candidates = this.database.prepare(`
      SELECT id, candidate_event_id, candidate_payload_hash, attempts, last_diagnostic
      FROM terminal_outbox WHERE state = 'candidate_in_flight' AND next_attempt_at <= ?
      ORDER BY created_at
    `).all(now) as CandidateRow[];
    for (const candidate of candidates) {
      await this.reconcileCandidate(options, candidate);
    }
    const rows = this.database.prepare(`
      SELECT id FROM terminal_outbox
      WHERE (state = 'ready' AND next_attempt_at <= ?)
         OR (state = 'aborted' AND next_attempt_at <= ?)
         OR (state = 'leased' AND lease_until <= ?)
      ORDER BY created_at
    `).all(now, now, now) as Array<{ id: string }>;
    for (const row of rows) {
      await this.deliver(options, row.id);
    }
  }

  nextWakeAt(): number | undefined {
    const row = this.database.prepare(`
      SELECT MIN(wake_at) AS wake_at FROM (
        SELECT next_attempt_at AS wake_at FROM terminal_outbox WHERE state = 'ready'
        UNION ALL
        SELECT next_attempt_at AS wake_at FROM terminal_outbox WHERE state = 'candidate_in_flight'
        UNION ALL
        SELECT next_attempt_at AS wake_at FROM terminal_outbox WHERE state = 'aborted'
        UNION ALL
        SELECT lease_until AS wake_at FROM terminal_outbox WHERE state = 'leased'
      )
    `).get() as { wake_at: number | null };
    return row.wake_at ?? undefined;
  }
}

export class TerminalOutboxController {
  readonly outbox: TerminalOutbox;
  private running = false;
  private closed = false;
  private timerHandle: unknown;
  private drainPromise: Promise<void> | undefined;
  private readonly deliveries = new Set<Promise<unknown>>();

  private constructor(
    outbox: TerminalOutbox,
    private readonly policy: TerminalOutboxPolicy,
    private readonly deliveryOptions: Omit<OutboxDeliveryOptions, "policy">,
  ) {
    this.outbox = outbox;
  }

  get reservationBytes(): number {
    return this.outbox.reservationBytes;
  }

  static async open(
    policy: TerminalOutboxPolicy,
    deliveryOptions: Omit<OutboxDeliveryOptions, "policy">,
  ): Promise<TerminalOutboxController> {
    return new TerminalOutboxController(await TerminalOutbox.open(policy), policy, deliveryOptions);
  }

  start(): void {
    if (this.closed) throw new Error("TERMINAL_OUTBOX_CONTROLLER_CLOSED");
    if (this.running) return;
    this.running = true;
    this.requestDrain();
  }

  async stop(): Promise<void> {
    if (this.closed) return;
    this.running = false;
    if (this.timerHandle !== undefined) {
      this.policy.timer.cancel(this.timerHandle);
      this.timerHandle = undefined;
    }
    await this.drainPromise;
    await Promise.all(this.deliveries);
    this.outbox.close();
    this.closed = true;
  }

  reserve(agentSessionId: string, executionId: string, profile: string): string | null {
    return this.outbox.reserve(agentSessionId, executionId, profile);
  }

  reserveCandidate(input: CandidateReservationInput): string | null {
    const id = this.outbox.reserveCandidate(input);
    this.scheduleNextWake();
    return id;
  }

  cancelReservation(id: string): void {
    this.outbox.cancelReservation(id);
  }

  bindAdmission(id: string, taskId: string, inputId: string): void {
    this.outbox.bindAdmission(id, taskId, inputId);
  }

  reconcileCandidateResponse(id: string, admission: Admission): void {
    this.outbox.reconcileCandidateResponse(id, admission);
    this.scheduleNextWake();
  }

  markAdmissionAborted(agentSessionId: string, executionId: string, reason: TerminalAbortReason): string {
    const id = this.outbox.markAdmissionAborted(agentSessionId, executionId, reason);
    this.scheduleNextWake();
    return id;
  }

  storeCompletionForAdmission(input: AdmittedTerminalInput): ReturnType<TerminalOutbox["storeCompletionForAdmission"]> {
    const stored = this.outbox.storeCompletionForAdmission(input);
    this.scheduleNextWake();
    return stored;
  }

  storeCompletion(id: string, input: TerminalEnvelopeInput): ReturnType<TerminalOutbox["storeCompletion"]> {
    const stored = this.outbox.storeCompletion(id, input);
    this.scheduleNextWake();
    return stored;
  }

  async deliver(id?: string): Promise<OutboxDelivery | undefined> {
    await this.drainPromise;
    const pending = this.outbox.deliver(this.deliveryOptions, id);
    this.deliveries.add(pending);
    try {
      return await pending;
    } finally {
      this.deliveries.delete(pending);
      this.scheduleNextWake();
    }
  }

  private requestDrain(): void {
    if (!this.running || this.drainPromise) return;
    if (this.timerHandle !== undefined) {
      this.policy.timer.cancel(this.timerHandle);
      this.timerHandle = undefined;
    }
    this.drainPromise = this.outbox.deliverDue(this.deliveryOptions)
      .catch(() => {
        // A later persisted wake-up retries recoverable delivery failures.
      })
      .finally(() => {
        this.drainPromise = undefined;
        this.scheduleNextWake();
      });
  }

  private scheduleNextWake(): void {
    if (!this.running || this.drainPromise) return;
    if (this.timerHandle !== undefined) {
      this.policy.timer.cancel(this.timerHandle);
      this.timerHandle = undefined;
    }
    const wakeAt = this.outbox.nextWakeAt();
    if (wakeAt === undefined) return;
    this.timerHandle = this.policy.timer.schedule(() => {
      this.timerHandle = undefined;
      this.requestDrain();
    }, Math.max(0, wakeAt - this.policy.clock()));
  }
}

const controllers = new Map<string, TerminalOutboxController>();
const pendingControllers = new Map<string, Promise<TerminalOutboxController>>();

function controllerKey(policy: TerminalOutboxPolicy): string {
  return join(resolve(policy.dataDir ?? resolveCrucibleDataDir()), "adapter-terminal-outbox.db");
}

export async function startTerminalOutboxController(
  policy: TerminalOutboxPolicy,
  deliveryOptions: Omit<OutboxDeliveryOptions, "policy">,
): Promise<TerminalOutboxController> {
  const key = controllerKey(policy);
  // Concurrent dispatches share one creation promise so a lost race cannot
  // leak a second singleton connection behind the map entry.
  let pending = pendingControllers.get(key);
  if (!pending) {
    pending = (async () => {
      let controller = controllers.get(key);
      if (!controller) {
        controller = await TerminalOutboxController.open(policy, deliveryOptions);
        controllers.set(key, controller);
      }
      return controller;
    })();
    pendingControllers.set(key, pending);
    pending.finally(() => {
      if (pendingControllers.get(key) === pending) pendingControllers.delete(key);
    }).catch(() => undefined);
  }
  const controller = await pending;
  controller.start();
  return controller;
}

export async function stopTerminalOutboxController(policy: TerminalOutboxPolicy): Promise<void> {
  const key = controllerKey(policy);
  const controller = controllers.get(key);
  if (!controller) return;
  controllers.delete(key);
  await controller.stop();
}
