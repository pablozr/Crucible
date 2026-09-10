import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";

import {
  TERMINAL_COMPATIBILITY_PROFILE,
  TERMINAL_OUTCOME,
  TERMINAL_SIGNAL,
  TerminalOutbox,
  TerminalOutboxController,
  createTerminalOutboxPolicy,
  dispatchOpenCodeV1,
  stopTerminalOutboxController,
  type TerminalOutboxPolicy,
} from "../src/opencode-v1/index.js";
import {
  createCanonicalCandidate,
  legacyNormalizedEnvelopeHash,
} from "../src/runtime/core-client.js";
import type { FetchImpl } from "../src/runtime/contracts.js";
import { loadSqliteDriver } from "../src/runtime/sqlite-driver.js";

const PROJECT_ID = "123e4567-e89b-42d3-a456-426614174000";
const EVENT_ID = "123e4567-e89b-42d3-a456-426614174001";
const COMPLETION_ID = "123e4567-e89b-42d3-a456-426614174002";
const NOW = Date.parse("2026-09-07T00:00:00.000Z");
const CORE_OPTIONS = { coreUrl: "http://core.test", timeoutMs: 2000 };

const uuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function policy(dataDir: string, overrides: Partial<TerminalOutboxPolicy> = {}): TerminalOutboxPolicy {
  return {
    maxBytes: 4096,
    reservationBytes: 2048,
    authorizationWindowMs: 60_000,
    leaseMs: 1000,
    backoffBaseMs: 10,
    backoffMaxMs: 100,
    clock: () => NOW,
    jitter: () => 0,
    timer: {
      schedule: (callback, delayMs) => setTimeout(callback, delayMs),
      cancel: (handle) => clearTimeout(handle as NodeJS.Timeout),
    },
    busyTimeoutMs: 1000,
    dataDir,
    ...overrides,
  };
}

function request() {
  return {
    openCodeVersion: "1.18.28",
    agentSessionId: "session-1",
    messageId: "input-1",
    executionId: "execution-1",
    workspacePath: "/repo",
    delivery: "new" as const,
  };
}

function admission() {
  return {
    status: "ok",
    data: { event: {
      event_id: EVENT_ID,
      status: "accepted",
      outcome: "admitted",
      input_id: "input-1",
      task_id: "task-1",
      dispatch_authorized: true,
    } },
  };
}

function detail(eventId: string, payloadHash: string, status: "accepted" | "rejected" | "processing" = "accepted") {
  return {
    status: "ok",
    data: { event: {
      event_id: eventId,
      status,
      outcome: status === "accepted" ? "completed" : status,
      input_id: "input-1",
      task_id: "task-1",
      dispatch_authorized: false,
      payload_hash: payloadHash,
    } },
  };
}

function completionInput() {
  return {
    eventId: COMPLETION_ID,
    occurredAt: "2026-09-07T00:00:01.000Z",
    adapter: "opencode-v1",
    adapterVersion: "0.1.0",
    agentSessionId: "session-1",
    inputId: "input-1",
    executionId: "execution-1",
    projectId: PROJECT_ID,
    gitRoot: "/repo",
    workspacePath: "/repo",
    taskId: "task-1",
    terminalSignal: TERMINAL_SIGNAL,
    terminalOutcome: TERMINAL_OUTCOME,
    compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
    terminalObservedAt: "2026-09-07T00:00:01.000Z",
  };
}

test("adapter persists capacity reservation before input_candidate", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-order-"));
  let reservedDuringPost = false;
  const fetchImpl: FetchImpl = async (_url, init) => {
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    if (body["event_type"] === "input_candidate") {
      const database = new DatabaseSync(join(dataDir, "adapter-terminal-outbox.db"), { readOnly: true });
      reservedDuringPost = (database.prepare("SELECT COUNT(*) AS count FROM terminal_outbox WHERE state = 'candidate_in_flight' AND candidate_envelope IS NOT NULL AND candidate_payload_hash IS NOT NULL").get() as { count: number }).count === 1;
      database.close();
      return new Response(JSON.stringify(admission()));
    }
    const hash = createHash("sha256").update(String(init?.body)).digest("hex");
    return new Response(JSON.stringify(detail(COMPLETION_ID, hash)));
  };

  const terminalPolicy = policy(dataDir);
  t.after(async () => {
    await stopTerminalOutboxController(terminalPolicy);
    rmSync(dataDir, { recursive: true, force: true });
  });
  await dispatchOpenCodeV1(request(), { dispatch: async () => "sent" }, {
    compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
    terminalPolicy,
    observeTerminal: async () => ({
      agentSessionId: "session-1",
      executionId: "execution-1",
      observedAt: "2026-09-07T00:00:01.000Z",
      signal: TERMINAL_SIGNAL,
      finish: TERMINAL_OUTCOME,
    }),
    testing: { fetchImpl, resolveProject: async () => ({ projectId: PROJECT_ID, gitRoot: "/repo" }), eventId: EVENT_ID, completionEventId: COMPLETION_ID },
  });
  assert.equal(reservedDuringPost, true);
});

test("lost candidate response is never cancelled or replayed and GET binds exact admission", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-candidate-reconcile-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  let now = NOW;
  const outbox = await TerminalOutbox.open(policy(dataDir, { clock: () => now }));
  const candidate = createCanonicalCandidate({
    agentSessionId: "session-1",
    messageId: "input-1",
    executionId: "execution-1",
    projectId: PROJECT_ID,
    gitRoot: "/repo",
    workspacePath: "/repo",
    delivery: "new",
  }, { adapter: "opencode-v1", adapterVersion: "0.1.0", eventId: EVENT_ID, occurredAt: "2026-09-07T00:00:00.000Z" });
  const reservation = outbox.reserveCandidate({
    candidate,
    agentSessionId: "session-1",
    executionId: "execution-1",
    projectId: PROJECT_ID,
    gitRoot: "/repo",
    workspacePath: "/repo",
    compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
    reconciliationDelayMs: 10,
  })!;
  now += 10;
  let posts = 0;
  let gets = 0;
  await outbox.deliverDue({ ...CORE_OPTIONS, fetchImpl: async (_url, init) => {
    if (init?.method === "POST") posts += 1;
    if (init?.method === "GET") gets += 1;
    return new Response(JSON.stringify({ status: "ok", data: { event: {
      event_id: EVENT_ID,
      status: "accepted",
      outcome: "admitted",
      input_id: "input-1",
      task_id: "task-1",
      dispatch_authorized: true,
      payload_hash: candidate.payloadHash,
    } } }));
  } });
  const database = new DatabaseSync(outbox.databasePath, { readOnly: true });
  const row = database.prepare("SELECT state, task_id, input_id, candidate_envelope FROM terminal_outbox WHERE id = ?").get(reservation) as Record<string, unknown>;
  database.close();
  outbox.close();
  assert.equal(posts, 0);
  assert.equal(gets, 1);
  assert.deepEqual([row["state"], row["task_id"], row["input_id"]], ["admitted", "task-1", "input-1"]);
  assert.equal(row["candidate_envelope"], candidate.envelope);
});

test("lost candidate response reconciles a legacy normalized Core hash", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-candidate-legacy-hash-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  let now = NOW;
  const outbox = await TerminalOutbox.open(policy(dataDir, { clock: () => now }));
  const candidate = createCanonicalCandidate({
    agentSessionId: "session-1", messageId: "input-1", executionId: "execution-1",
    projectId: PROJECT_ID, gitRoot: "/repo", workspacePath: "/repo", delivery: "new",
  }, { adapter: "opencode-v1", adapterVersion: "0.1.0", eventId: EVENT_ID,
    occurredAt: "2026-09-07T00:00:00.000Z" });
  assert.equal(
    legacyNormalizedEnvelopeHash(candidate.envelope),
    process.platform === "win32"
      ? "e397958ed92e545122c8aadfcb76fd436f647a3d766b27e4089720f47322d8f7"
      : "20d8780e4623d69f44da66d4603b713db68959859f40afe40a8e5fe036315a0e",
  );
  const reservation = outbox.reserveCandidate({ candidate, agentSessionId: "session-1",
    executionId: "execution-1", projectId: PROJECT_ID, gitRoot: "/repo",
    workspacePath: "/repo", compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
    reconciliationDelayMs: 1 })!;
  now += 1;

  await outbox.deliverDue({ ...CORE_OPTIONS, fetchImpl: async () => new Response(JSON.stringify({
    status: "ok", data: { event: { event_id: EVENT_ID, status: "accepted",
      outcome: "admitted", input_id: "input-1", task_id: "task-1",
      dispatch_authorized: true,
      payload_hash: legacyNormalizedEnvelopeHash(candidate.envelope) } },
  })) });

  const database = new DatabaseSync(outbox.databasePath, { readOnly: true });
  const row = database.prepare("SELECT state, task_id FROM terminal_outbox WHERE id = ?")
    .get(reservation) as { state: string; task_id: string };
  database.close();
  outbox.close();
  assert.equal(row.state, "admitted");
  assert.equal(row.task_id, "task-1");
});

test("candidate GET hash mismatch remains reserved", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-candidate-mismatch-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  let now = NOW;
  const outbox = await TerminalOutbox.open(policy(dataDir, { clock: () => now }));
  const candidate = createCanonicalCandidate({
    agentSessionId: "session-1", messageId: "input-1", executionId: "execution-1",
    projectId: PROJECT_ID, gitRoot: "/repo", workspacePath: "/repo", delivery: "new",
  }, { adapter: "opencode-v1", adapterVersion: "0.1.0", eventId: EVENT_ID });
  outbox.reserveCandidate({ candidate, agentSessionId: "session-1", executionId: "execution-1",
    projectId: PROJECT_ID, gitRoot: "/repo", workspacePath: "/repo",
    compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE, reconciliationDelayMs: 1 });
  now += 1;
  await outbox.deliverDue({ ...CORE_OPTIONS, fetchImpl: async () => new Response(JSON.stringify({
    status: "ok", data: { event: { event_id: EVENT_ID, status: "rejected", outcome: "rejected",
      dispatch_authorized: false, payload_hash: "wrong" } },
  })) });
  const database = new DatabaseSync(outbox.databasePath, { readOnly: true });
  const row = database.prepare("SELECT state, accounted_bytes FROM terminal_outbox").get() as { state: string; accounted_bytes: number };
  database.close();
  outbox.close();
  assert.equal(row.state, "candidate_in_flight");
  assert.equal(row.accounted_bytes, 2048);
});

test("candidate reserve releases only after repeated exact Core not-found", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-candidate-not-found-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  let now = NOW;
  const outbox = await TerminalOutbox.open(policy(dataDir, { clock: () => now }));
  const candidate = createCanonicalCandidate({
    agentSessionId: "session-1", messageId: "input-1", executionId: "execution-1",
    projectId: PROJECT_ID, gitRoot: "/repo", workspacePath: "/repo", delivery: "new",
  }, { adapter: "opencode-v1", adapterVersion: "0.1.0", eventId: EVENT_ID });
  outbox.reserveCandidate({ candidate, agentSessionId: "session-1", executionId: "execution-1",
    projectId: PROJECT_ID, gitRoot: "/repo", workspacePath: "/repo",
    compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE, reconciliationDelayMs: 1 });
  const notFound = async () => new Response(JSON.stringify({
    status: "error", data: { code: "EVENT_NOT_FOUND" },
  }), { status: 404 });
  now += 1;
  await outbox.deliverDue({ ...CORE_OPTIONS, fetchImpl: notFound });
  let database = new DatabaseSync(outbox.databasePath, { readOnly: true });
  let row = database.prepare("SELECT state, accounted_bytes, next_attempt_at FROM terminal_outbox").get() as {
    state: string; accounted_bytes: number; next_attempt_at: number;
  };
  database.close();
  assert.equal(row.state, "candidate_in_flight");
  assert.equal(row.accounted_bytes, 2048);

  now = row.next_attempt_at;
  await outbox.deliverDue({ ...CORE_OPTIONS, fetchImpl: notFound });
  database = new DatabaseSync(outbox.databasePath, { readOnly: true });
  row = database.prepare("SELECT state, accounted_bytes, next_attempt_at FROM terminal_outbox").get() as typeof row;
  database.close();
  outbox.close();
  assert.equal(row.state, "terminal");
  assert.equal(row.accounted_bytes, 0);
});

test("bind does not start authorization window and completion persists observation atomically", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-window-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  let now = NOW;
  const outbox = await TerminalOutbox.open(policy(dataDir, {
    authorizationWindowMs: 60_000,
    clock: () => now,
  }));
  const reservation = outbox.reserve("session-1", "execution-1", TERMINAL_COMPATIBILITY_PROFILE)!;

  now += 10 * 60_000;
  outbox.bindAdmission(reservation, "task-1", "input-1");
  let database = new DatabaseSync(outbox.databasePath, { readOnly: true });
  const admitted = database.prepare(
    "SELECT terminal_observed_at, capture_not_after FROM terminal_outbox WHERE id = ?",
  ).get(reservation) as { terminal_observed_at: string | null; capture_not_after: string | null };
  database.close();
  assert.equal(admitted.terminal_observed_at, null);
  assert.equal(admitted.capture_not_after, null);

  now += 10 * 60_000;
  outbox.storeCompletion(reservation, completionInput());
  database = new DatabaseSync(outbox.databasePath, { readOnly: true });
  const ready = database.prepare(
    "SELECT terminal_observed_at, capture_not_after, envelope FROM terminal_outbox WHERE id = ?",
  ).get(reservation) as { terminal_observed_at: string; capture_not_after: string; envelope: string };
  database.close();
  outbox.close();

  assert.equal(ready.terminal_observed_at, "2026-09-07T00:00:01.000Z");
  assert.equal(ready.capture_not_after, "2026-09-07T00:01:01.000Z");
  assert.equal(JSON.parse(ready.envelope).payload.capture_not_after, ready.capture_not_after);
});

test("legacy outbox database gains capture_not_after before any completion write", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-legacy-migration-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  const legacy = new DatabaseSync(join(dataDir, "adapter-terminal-outbox.db"));
  legacy.exec(`
    CREATE TABLE terminal_outbox (
      id TEXT PRIMARY KEY,
      state TEXT NOT NULL,
      reserved_bytes INTEGER NOT NULL,
      accounted_bytes INTEGER NOT NULL,
      created_at INTEGER NOT NULL,
      agent_session_id TEXT NOT NULL,
      execution_id TEXT NOT NULL,
      compatibility_profile TEXT NOT NULL,
      task_id TEXT,
      input_id TEXT,
      event_id TEXT UNIQUE,
      envelope TEXT,
      payload_hash TEXT,
      attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt_at INTEGER,
      lease_until INTEGER,
      last_diagnostic TEXT
    ) STRICT;
  `);
  legacy.close();

  const outbox = await TerminalOutbox.open(policy(dataDir));
  try {
    const reservation = outbox.reserve("session-1", "execution-1", TERMINAL_COMPATIBILITY_PROFILE)!;
    outbox.bindAdmission(reservation, "task-1", "input-1");
    outbox.storeCompletion(reservation, completionInput());

    const database = new DatabaseSync(outbox.databasePath, { readOnly: true });
    const columns = database.prepare("PRAGMA table_info(terminal_outbox)").all() as Array<{ name: string }>;
    const row = database.prepare(
      "SELECT capture_not_after, envelope FROM terminal_outbox WHERE id = ?",
    ).get(reservation) as { capture_not_after: string; envelope: string };
    database.close();
    assert.ok(columns.some((column) => column.name === "capture_not_after"));
    assert.equal(row.capture_not_after, "2026-09-07T00:01:01.000Z");
    assert.equal(JSON.parse(row.envelope).payload.capture_not_after, row.capture_not_after);
  } finally {
    outbox.close();
  }
});

test("capacity exhaustion skips Core candidate but continues OpenCode dispatch", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-capacity-"));
  const first = await TerminalOutbox.open(policy(dataDir, { maxBytes: 2048, reservationBytes: 2048 }));
  assert.ok(first.reserve("other", "other", TERMINAL_COMPATIBILITY_PROFILE));
  first.close();
  let fetchCalls = 0;
  let context: unknown;
  const terminalPolicy = policy(dataDir, { maxBytes: 2048, reservationBytes: 2048 });
  t.after(async () => {
    await stopTerminalOutboxController(terminalPolicy);
    rmSync(dataDir, { recursive: true, force: true });
  });
  const result = await dispatchOpenCodeV1(request(), { dispatch: async (value) => { context = value; return "sent"; } }, {
    compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
    terminalPolicy,
    observeTerminal: async () => null,
    testing: { fetchImpl: async () => { fetchCalls += 1; throw new Error("unexpected"); }, resolveProject: async () => ({ projectId: PROJECT_ID, gitRoot: "/repo" }), eventId: EVENT_ID },
  });
  assert.equal(fetchCalls, 0);
  assert.equal(result.dispatchResult, "sent");
  assert.equal(result.tracked, false);
  assert.equal((context as { diagnostic: string }).diagnostic, "TERMINAL_OUTBOX_CAPACITY_EXHAUSTED");
});

test("ambiguous admission preserves candidate reservation", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-untracked-"));
  const terminalPolicy = policy(dataDir, { maxBytes: 2048 });
  t.after(async () => {
    await stopTerminalOutboxController(terminalPolicy);
    rmSync(dataDir, { recursive: true, force: true });
  });
  const result = await dispatchOpenCodeV1(request(), { dispatch: async () => "sent" }, {
    compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
    terminalPolicy,
    observeTerminal: async () => null,
    testing: {
      fetchImpl: async () => { throw new Error("Core unavailable"); },
      resolveProject: async () => ({ projectId: PROJECT_ID, gitRoot: "/repo" }),
      eventId: EVENT_ID,
    },
  });

  assert.equal(result.tracked, false);
  const outbox = await TerminalOutbox.open(policy(dataDir, { maxBytes: 2048 }));
  assert.equal(outbox.reserve("new", "new", TERMINAL_COMPATIBILITY_PROFILE), null);
  const database = new DatabaseSync(outbox.databasePath, { readOnly: true });
  const row = database.prepare("SELECT state FROM terminal_outbox").get() as { state: string };
  database.close();
  assert.equal(row.state, "candidate_in_flight");
  outbox.close();
});

test("dispatch failure keeps admitted reservation durable", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-dispatch-failure-"));
  const terminalPolicy = policy(dataDir, { maxBytes: 2048 });
  t.after(async () => {
    await stopTerminalOutboxController(terminalPolicy);
    rmSync(dataDir, { recursive: true, force: true });
  });
  await assert.rejects(
    dispatchOpenCodeV1(request(), { dispatch: async () => { throw new Error("dispatch failed"); } }, {
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      terminalPolicy,
      observeTerminal: async () => null,
      testing: {
        fetchImpl: async () => new Response(JSON.stringify(admission())),
        resolveProject: async () => ({ projectId: PROJECT_ID, gitRoot: "/repo" }),
        eventId: EVENT_ID,
      },
    }),
    /dispatch failed/,
  );

  const database = new DatabaseSync(join(dataDir, "adapter-terminal-outbox.db"), { readOnly: true });
  const row = database.prepare(
    "SELECT state, task_id, input_id, abort_event_id, abort_payload_hash, abort_reason, abort_envelope FROM terminal_outbox",
  ).get() as {
    state: string;
    task_id: string;
    input_id: string;
    abort_event_id: string;
    abort_payload_hash: string;
    abort_reason: string;
    abort_envelope: string;
  };
  database.close();
  assert.deepEqual([row.state, row.task_id, row.input_id], ["aborted", "task-1", "input-1"]);
  assert.match(row.abort_event_id, uuidPattern);
  assert.equal(row.abort_reason, "DISPATCH_FAILED");
  assert.match(row.abort_payload_hash, /^[0-9a-f]{64}$/);
  const envelope = JSON.parse(row.abort_envelope) as Record<string, unknown>;
  assert.equal(envelope["event_type"], "task_finalization_aborted");
  assert.equal(envelope["event_id"], row.abort_event_id);
});

test("persisted abort is never replaced by a later terminal observation", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-abort-hook-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  const { outbox, reservation } = await abortedRow(dataDir, "DISPATCH_FAILED");
  try {
    assert.equal(outbox.reserve("new", "new", TERMINAL_COMPATIBILITY_PROFILE), null);
    const seeded = abortRow(outbox, reservation);

    const { inputId: _inputId, projectId: _projectId, gitRoot: _gitRoot,
      workspacePath: _workspacePath, taskId: _taskId, ...terminal } = completionInput();
    assert.throws(() => outbox.storeCompletionForAdmission(terminal), /TERMINAL_ABORT_PERSISTED/);

    const row = abortRow(outbox, reservation);
    assert.deepEqual([row.state, row.accounted_bytes, row.attempts],
      [seeded.state, 2048, seeded.attempts]);
    assert.equal(row.abort_event_id, seeded.abort_event_id);
    assert.equal(row.abort_envelope, seeded.abort_envelope);
    assert.equal(row.abort_payload_hash, seeded.abort_payload_hash);
    assert.equal(row.abort_reason, seeded.abort_reason);
  } finally {
    outbox.close();
  }
});

async function abortedRow(
  dataDir: string,
  reason: "DISPATCH_FAILED" | "TERMINAL_OBSERVER_FAILED" | "TERMINAL_SIGNAL_MISMATCH",
) {
  const outbox = await TerminalOutbox.open(policy(dataDir, { maxBytes: 2048 }));
  const candidate = createCanonicalCandidate({
    agentSessionId: "session-1", messageId: "input-1", executionId: "execution-1",
    projectId: PROJECT_ID, gitRoot: "/repo", workspacePath: "/repo", delivery: "new",
  }, { adapter: "opencode-v1", adapterVersion: "0.1.0", eventId: EVENT_ID });
  const reservation = outbox.reserveCandidate({ candidate, agentSessionId: "session-1",
    executionId: "execution-1", projectId: PROJECT_ID, gitRoot: "/repo", workspacePath: "/repo",
    compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE, reconciliationDelayMs: 2000 })!;
  outbox.bindAdmission(reservation, "task-1", "input-row-1");
  outbox.markAdmissionAborted("session-1", "execution-1", reason);
  return { outbox, reservation };
}

function abortRow(outbox: TerminalOutbox, reservation: string) {
  const database = new DatabaseSync(outbox.databasePath, { readOnly: true });
  const row = database.prepare(`
    SELECT state, task_id, input_id, accounted_bytes, attempts, abort_event_id,
           abort_envelope, abort_payload_hash, abort_reason
    FROM terminal_outbox WHERE id = ?
  `).get(reservation) as {
    state: string;
    task_id: string;
    input_id: string;
    accounted_bytes: number;
    attempts: number;
    abort_event_id: string;
    abort_envelope: string;
    abort_payload_hash: string;
    abort_reason: string;
  };
  database.close();
  return row;
}

test("each abort reason persists stable bytes and releases only on correlated ACK", async () => {
  for (const reason of ["DISPATCH_FAILED", "TERMINAL_OBSERVER_FAILED", "TERMINAL_SIGNAL_MISMATCH"] as const) {
    const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-abort-reason-"));
    const { outbox, reservation } = await abortedRow(dataDir, reason);
    try {
      const row = abortRow(outbox, reservation);
      assert.equal(row.state, "aborted");
      assert.equal(row.abort_reason, reason);
      assert.equal(row.input_id, "input-row-1");
      assert.match(row.abort_event_id, uuidPattern);
      const envelope = JSON.parse(row.abort_envelope) as Record<string, unknown>;
      assert.equal(envelope["event_type"], "task_finalization_aborted");
      assert.equal(envelope["event_id"], row.abort_event_id);
      assert.equal(envelope["adapter"], "opencode-v1");
      assert.equal(envelope["adapter_version"], "0.1.0");
      assert.equal(envelope["agent_session_id"], "session-1");
      assert.equal(envelope["execution_id"], "execution-1");
      assert.equal(envelope["project_id"], PROJECT_ID);
      assert.equal(envelope["git_root"], "/repo");
      assert.equal(envelope["workspace_path"], "/repo");
      assert.equal(envelope["input_id"], "input-1");
      assert.notEqual(envelope["input_id"], row.input_id);
      assert.deepEqual(envelope["payload"], { task_id: "task-1", abort_reason: reason });

      let posted: Record<string, unknown> | undefined;
      const delivery = await outbox.deliver({ ...CORE_OPTIONS, fetchImpl: async (_url, init) => {
        if (init?.method === "POST") {
          posted = JSON.parse(String(init?.body)) as Record<string, unknown>;
          return new Response(JSON.stringify({ status: "ok", data: { event: {
            event_id: posted["event_id"], status: "rejected", outcome: "rejected",
            input_id: "input-row-1", task_id: "task-1", dispatch_authorized: false,
          } } }));
        }
        return new Response(JSON.stringify({ status: "error", data: { code: "EVENT_NOT_FOUND" } }), { status: 404 });
      }}, reservation);

      assert.equal(posted?.["event_id"], row.abort_event_id);
      assert.equal(delivery?.terminal, true);
      assert.equal(delivery?.accepted, false);
      assert.equal(delivery?.diagnostic, reason);
      const released = abortRow(outbox, reservation);
      assert.equal(released.state, "terminal");
      assert.equal(released.accounted_bytes, 0);
      assert.ok(outbox.reserve("new", "new", TERMINAL_COMPATIBILITY_PROFILE));
    } finally {
      outbox.close();
      rmSync(dataDir, { recursive: true, force: true });
    }
  }
});

test("ambiguous lost abort ACK then observation retries only the same abort", async () => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-abort-ambiguous-"));
  let now = NOW;
  const outbox = await TerminalOutbox.open(policy(dataDir, { maxBytes: 2048, clock: () => now }));
  try {
    const candidate = createCanonicalCandidate({
      agentSessionId: "session-1", messageId: "input-1", executionId: "execution-1",
      projectId: PROJECT_ID, gitRoot: "/repo", workspacePath: "/repo", delivery: "new",
    }, { adapter: "opencode-v1", adapterVersion: "0.1.0", eventId: EVENT_ID });
    const reservation = outbox.reserveCandidate({ candidate, agentSessionId: "session-1",
      executionId: "execution-1", projectId: PROJECT_ID, gitRoot: "/repo", workspacePath: "/repo",
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE, reconciliationDelayMs: 2000 })!;
    outbox.bindAdmission(reservation, "task-1", "input-row-1");
    outbox.markAdmissionAborted("session-1", "execution-1", "DISPATCH_FAILED");
    const seeded = abortRow(outbox, reservation);

    await outbox.deliver({ ...CORE_OPTIONS, fetchImpl: async () => { throw new Error("lost abort ACK"); } }, reservation);
    let row = abortRow(outbox, reservation);
    assert.equal(row.abort_event_id, seeded.abort_event_id);
    assert.equal(row.abort_envelope, seeded.abort_envelope);
    assert.ok(row.attempts >= 1);

    const { inputId: _inputId, projectId: _projectId, gitRoot: _gitRoot,
      workspacePath: _workspacePath, taskId: _taskId, ...terminal } = completionInput();
    assert.throws(() => outbox.storeCompletionForAdmission(terminal), /TERMINAL_ABORT_PERSISTED/);
    row = abortRow(outbox, reservation);
    assert.equal(row.abort_event_id, seeded.abort_event_id);
    assert.equal(row.abort_envelope, seeded.abort_envelope);

    const pending = new DatabaseSync(outbox.databasePath, { readOnly: true });
    const scheduled = pending.prepare(
      "SELECT next_attempt_at FROM terminal_outbox WHERE id = ?",
    ).get(reservation) as { next_attempt_at: number };
    pending.close();
    now = scheduled.next_attempt_at;

    const posted: Array<Record<string, unknown>> = [];
    await outbox.deliverDue({ ...CORE_OPTIONS, fetchImpl: async (_url, init) => {
      if (init?.method === "POST") {
        posted.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        return new Response(JSON.stringify({ status: "ok", data: { event: {
          event_id: seeded.abort_event_id, status: "rejected", outcome: "rejected",
          input_id: "input-row-1", task_id: "task-1", dispatch_authorized: false,
        } } }));
      }
      return new Response(JSON.stringify({ status: "error", data: { code: "EVENT_NOT_FOUND" } }), { status: 404 });
    } });

    assert.equal(posted.length, 1);
    assert.equal(posted[0]!["event_id"], seeded.abort_event_id);
    assert.equal(posted[0]!["event_type"], "task_finalization_aborted");
    const database = new DatabaseSync(outbox.databasePath, { readOnly: true });
    const final = database.prepare(
      "SELECT state, event_id, envelope, abort_event_id, abort_envelope FROM terminal_outbox WHERE id = ?",
    ).get(reservation) as {
      state: string;
      event_id: string | null;
      envelope: string | null;
      abort_event_id: string;
      abort_envelope: string;
    };
    database.close();
    assert.equal(final.state, "terminal");
    assert.equal(final.event_id, null);
    assert.equal(final.envelope, null);
    assert.equal(final.abort_event_id, seeded.abort_event_id);
    assert.equal(final.abort_envelope, seeded.abort_envelope);
  } finally {
    outbox.close();
    rmSync(dataDir, { recursive: true, force: true });
  }
});

test("abort delivery survives lost response and restart with stable id and bytes", async () => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-abort-restart-"));
  const first = await abortedRow(dataDir, "TERMINAL_SIGNAL_MISMATCH");
  try {
    const seeded = abortRow(first.outbox, first.reservation);

    let firstPosts = 0;
    await first.outbox.deliver({ ...CORE_OPTIONS,
      fetchImpl: async (_url, init) => {
        if (init?.method === "POST") firstPosts += 1;
        throw new Error("lost");
      },
    }, first.reservation);
    const retained = abortRow(first.outbox, first.reservation);
    assert.equal(firstPosts, 1);
    assert.equal(retained.accounted_bytes, 2048);
    assert.equal(retained.abort_envelope, seeded.abort_envelope);
    first.outbox.close();

    const second = await TerminalOutbox.open(policy(dataDir, { maxBytes: 2048 }));
    try {
      let retryEnvelope = "";
      const delivery = await second.deliver({ ...CORE_OPTIONS,
        fetchImpl: async (_url, init) => {
          if (init?.method === "POST") {
            retryEnvelope = String(init.body);
            throw new Error("lost again");
          }
          return new Response(JSON.stringify({ status: "ok", data: { event: {
            event_id: seeded.abort_event_id, status: "rejected", outcome: "rejected",
            input_id: "input-row-1", task_id: "task-1", dispatch_authorized: false,
            payload_hash: seeded.abort_payload_hash,
            failure_code: "TERMINAL_SIGNAL_MISMATCH",
          } } }));
        },
      }, first.reservation);
      assert.equal(retryEnvelope, seeded.abort_envelope);
      assert.equal(delivery?.terminal, true);
      assert.equal(delivery?.accepted, false);
      const released = abortRow(second, first.reservation);
      assert.equal(released.state, "terminal");
      assert.equal(released.accounted_bytes, 0);
    } finally {
      second.close();
    }
  } finally {
    rmSync(dataDir, { recursive: true, force: true });
  }
});

test("abort lost response reconciles a legacy normalized Core hash", async () => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-abort-legacy-hash-"));
  const { outbox, reservation } = await abortedRow(dataDir, "TERMINAL_SIGNAL_MISMATCH");
  try {
    const seeded = abortRow(outbox, reservation);
    const delivery = await outbox.deliver({ ...CORE_OPTIONS,
      fetchImpl: async (_url, init) => {
        if (init?.method === "POST") throw new Error("lost response");
        return new Response(JSON.stringify({ status: "ok", data: { event: {
          event_id: seeded.abort_event_id, status: "rejected", outcome: "rejected",
          input_id: "input-row-1", task_id: "task-1", dispatch_authorized: false,
          payload_hash: legacyNormalizedEnvelopeHash(seeded.abort_envelope),
          failure_code: "TERMINAL_SIGNAL_MISMATCH",
        } } }));
      },
    }, reservation);
    assert.equal(delivery?.terminal, true);
    assert.equal(delivery?.accepted, false);
    assert.equal(abortRow(outbox, reservation).accounted_bytes, 0);
  } finally {
    outbox.close();
    rmSync(dataDir, { recursive: true, force: true });
  }
});

test("uncorrelated abort responses retain the durable abort", async () => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-abort-mismatch-"));
  const { outbox, reservation } = await abortedRow(dataDir, "TERMINAL_OBSERVER_FAILED");
  try {
    const seeded = abortRow(outbox, reservation);
    const rejectedBody = {
      status: "ok",
      data: { event: {
        event_id: "wrong-id", status: "rejected", outcome: "rejected",
        input_id: "input-row-1", task_id: "task-1", dispatch_authorized: false,
      } },
    };
    const delivery = await outbox.deliver({ ...CORE_OPTIONS,
      fetchImpl: async (_url, init) => new Response(JSON.stringify(
        init?.method === "POST"
          ? rejectedBody
          : { status: "ok", data: { event: { ...rejectedBody.data.event, payload_hash: "wrong" } } },
      )),
    }, reservation);
    assert.equal(delivery?.terminal, false);

    const acceptedBody = {
      status: "ok",
      data: { event: {
        event_id: seeded.abort_event_id, status: "accepted", outcome: "completed",
        input_id: "input-row-1", task_id: "task-1", dispatch_authorized: false,
      } },
    };
    const accepted = await outbox.deliver({ ...CORE_OPTIONS,
      fetchImpl: async () => new Response(JSON.stringify(acceptedBody)),
    }, reservation);
    assert.equal(accepted?.terminal, false);

    const row = abortRow(outbox, reservation);
    assert.equal(row.accounted_bytes, 2048);
    assert.equal(row.abort_envelope, seeded.abort_envelope);
    assert.equal(row.abort_payload_hash, seeded.abort_payload_hash);
    assert.equal(row.abort_reason, "TERMINAL_OBSERVER_FAILED");
    assert.ok(row.attempts >= 2);
    assert.equal(outbox.reserve("new", "new", TERMINAL_COMPATIBILITY_PROFILE), null);
  } finally {
    outbox.close();
    rmSync(dataDir, { recursive: true, force: true });
  }
});

test("retry after restart reuses immutable event id and bytes and reconciles lost response", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-restart-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  const first = await TerminalOutbox.open(policy(dataDir));
  const reservation = first.reserve("session-1", "execution-1", TERMINAL_COMPATIBILITY_PROFILE)!;
  first.bindAdmission(reservation, "task-1", "input-1");
  const stored = first.storeCompletion(reservation, completionInput());
  let firstBytes = "";
  await first.deliver({ ...CORE_OPTIONS,
    fetchImpl: async (_url, init) => {
      if (init?.method === "POST") firstBytes = String(init.body);
      throw new Error("lost");
    },
  }, reservation);
  first.close();

  const second = await TerminalOutbox.open(policy(dataDir));
  let retryBytes = "";
  const delivery = await second.deliver({ ...CORE_OPTIONS,
    fetchImpl: async (url, init) => {
      if (init?.method === "POST") {
        retryBytes = String(init.body);
        return new Response(JSON.stringify({ status: "ok", data: { event: { status: "processing" } } }));
      }
      assert.match(url, new RegExp(`${COMPLETION_ID}$`));
      return new Response(JSON.stringify(detail(COMPLETION_ID, stored.payloadHash)));
    },
  }, reservation);
  second.close();
  assert.equal(retryBytes, firstBytes);
  assert.equal(JSON.parse(retryBytes).event_id, COMPLETION_ID);
  assert.equal(delivery?.terminal, true);
});

test("completion lost response reconciles a legacy normalized Core hash", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-completion-legacy-hash-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  const outbox = await TerminalOutbox.open(policy(dataDir));
  const reservation = outbox.reserve("session-1", "execution-1", TERMINAL_COMPATIBILITY_PROFILE)!;
  outbox.bindAdmission(reservation, "task-1", "input-1");
  outbox.storeCompletion(reservation, completionInput());
  let envelope = "";

  const delivery = await outbox.deliver({ ...CORE_OPTIONS,
    fetchImpl: async (_url, init) => {
      if (init?.method === "POST") {
        envelope = String(init.body);
        throw new Error("lost response");
      }
      return new Response(JSON.stringify(
        detail(COMPLETION_ID, legacyNormalizedEnvelopeHash(envelope)),
      ));
    },
  }, reservation);
  outbox.close();
  assert.equal(delivery?.terminal, true);
  assert.equal(delivery?.accepted, true);
});

test("started controller retries elapsed backoff without another dispatch", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-controller-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  let now = NOW;
  let scheduled: { callback: () => void; delayMs: number; cancelled: boolean } | undefined;
  const controllerPolicy = policy(dataDir, {
    clock: () => now,
    jitter: (maximum) => maximum,
    timer: {
      schedule: (callback, delayMs) => {
        scheduled = { callback, delayMs, cancelled: false };
        return scheduled;
      },
      cancel: (handle) => {
        (handle as { cancelled: boolean }).cancelled = true;
      },
    },
  });
  const seed = await TerminalOutbox.open(controllerPolicy);
  const reservation = seed.reserve("session-1", "execution-1", TERMINAL_COMPATIBILITY_PROFILE)!;
  seed.bindAdmission(reservation, "task-1", "input-1");
  const stored = seed.storeCompletion(reservation, completionInput());
  await seed.deliver({ ...CORE_OPTIONS, fetchImpl: async () => { throw new Error("offline"); } }, reservation);
  seed.close();

  let posts = 0;
  const controller = await TerminalOutboxController.open(controllerPolicy, { ...CORE_OPTIONS,
    fetchImpl: async (_url, init) => {
      if (init?.method === "POST") posts += 1;
      return new Response(JSON.stringify(detail(COMPLETION_ID, stored.payloadHash)));
    },
  });
  controller.start();
  await Promise.resolve();
  await Promise.resolve();

  assert.equal(posts, 0);
  assert.equal(scheduled?.delayMs, 10);
  now += 10;
  scheduled?.callback();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  assert.equal(posts, 1);
  await controller.stop();
  const database = new DatabaseSync(join(dataDir, "adapter-terminal-outbox.db"), { readOnly: true });
  const row = database.prepare("SELECT state FROM terminal_outbox WHERE id = ?").get(reservation) as { state: string };
  database.close();
  assert.equal(row.state, "terminal");
});

test("controller restart immediately recovers an expired lease", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-lease-recovery-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  let now = NOW;
  const controllerPolicy = policy(dataDir, {
    clock: () => now,
    timer: {
      schedule: () => ({ id: 1 }),
      cancel: () => {},
    },
  });
  const seed = await TerminalOutbox.open(controllerPolicy);
  const reservation = seed.reserve("session-1", "execution-1", TERMINAL_COMPATIBILITY_PROFILE)!;
  seed.bindAdmission(reservation, "task-1", "input-1");
  const stored = seed.storeCompletion(reservation, completionInput());
  const database = new DatabaseSync(seed.databasePath);
  database.prepare("UPDATE terminal_outbox SET state = 'leased', lease_until = ? WHERE id = ?")
    .run(NOW + 1000, reservation);
  database.close();
  seed.close();
  now += 1000;

  let posts = 0;
  const controller = await TerminalOutboxController.open(controllerPolicy, { ...CORE_OPTIONS,
    fetchImpl: async () => {
      posts += 1;
      return new Response(JSON.stringify(detail(COMPLETION_ID, stored.payloadHash)));
    },
  });
  controller.start();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  assert.equal(posts, 1);
  await controller.stop();
});

test("direct POST confirms without payload hash but GET reconciliation requires it", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-confirmation-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));

  const directOutbox = await TerminalOutbox.open(policy(dataDir));
  const directReservation = directOutbox.reserve("session-1", "execution-1", TERMINAL_COMPATIBILITY_PROFILE)!;
  directOutbox.bindAdmission(directReservation, "task-1", "input-1");
  directOutbox.storeCompletion(directReservation, completionInput());
  let directGets = 0;
  const direct = await directOutbox.deliver({ ...CORE_OPTIONS,
    fetchImpl: async (_url, init) => {
      if (init?.method === "GET") directGets += 1;
      return new Response(JSON.stringify({
        status: "ok",
        data: { event: {
          event_id: COMPLETION_ID,
          status: "accepted",
          outcome: "completed",
          input_id: "input-1",
          task_id: "task-1",
          dispatch_authorized: false,
        } },
      }));
    },
  }, directReservation);
  directOutbox.close();
  assert.equal(direct?.terminal, true);
  assert.equal(directGets, 0);

  const reconcileDir = mkdtempSync(join(tmpdir(), "crucible-outbox-reconcile-hash-"));
  t.after(() => rmSync(reconcileDir, { recursive: true, force: true }));
  const reconcileOutbox = await TerminalOutbox.open(policy(reconcileDir));
  const reconcileReservation = reconcileOutbox.reserve("session-1", "execution-1", TERMINAL_COMPATIBILITY_PROFILE)!;
  reconcileOutbox.bindAdmission(reconcileReservation, "task-1", "input-1");
  reconcileOutbox.storeCompletion(reconcileReservation, completionInput());
  const reconciled = await reconcileOutbox.deliver({ ...CORE_OPTIONS,
    fetchImpl: async (_url, init) => new Response(JSON.stringify(
      init?.method === "POST"
        ? completionInput()
        : detail(COMPLETION_ID, "wrong-hash"),
    )),
  }, reconcileReservation);
  reconcileOutbox.close();
  assert.equal(reconciled?.terminal, false);
  assert.equal(reconciled?.diagnostic, "COMPLETION_UNCONFIRMED");
});

test("processing and identity mismatch remain durable and unresolved entries are never evicted", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-processing-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  const outbox = await TerminalOutbox.open(policy(dataDir, { maxBytes: 2048 }));
  const reservation = outbox.reserve("session-1", "execution-1", TERMINAL_COMPATIBILITY_PROFILE)!;
  outbox.bindAdmission(reservation, "task-1", "input-1");
  const stored = outbox.storeCompletion(reservation, completionInput());
  const delivery = await outbox.deliver({ ...CORE_OPTIONS, fetchImpl: async () => new Response(JSON.stringify(detail(COMPLETION_ID, stored.payloadHash, "processing"))) }, reservation);
  assert.equal(delivery?.terminal, false);
  assert.equal(outbox.reserve("new", "new", TERMINAL_COMPATIBILITY_PROFILE), null);
  outbox.close();
});

test("rejected terminal acknowledgement releases reserved capacity", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-rejected-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  const outbox = await TerminalOutbox.open(policy(dataDir, { maxBytes: 2048 }));
  const reservation = outbox.reserve("session-1", "execution-1", TERMINAL_COMPATIBILITY_PROFILE)!;
  outbox.bindAdmission(reservation, "task-1", "input-1");
  const stored = outbox.storeCompletion(reservation, completionInput());
  const delivery = await outbox.deliver({ ...CORE_OPTIONS,
    fetchImpl: async () => new Response(JSON.stringify(detail(COMPLETION_ID, stored.payloadHash, "rejected"))),
  }, reservation);
  assert.equal(delivery?.terminal, true);
  assert.equal(delivery?.accepted, false);
  assert.ok(outbox.reserve("new", "new", TERMINAL_COMPATIBILITY_PROFILE));
  outbox.close();
});
test("terminal observation identity mismatch creates no completion envelope", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-identity-"));
  let posts = 0;
  const terminalPolicy = policy(dataDir);
  t.after(async () => {
    await stopTerminalOutboxController(terminalPolicy);
    rmSync(dataDir, { recursive: true, force: true });
  });
  const result = await dispatchOpenCodeV1(request(), { dispatch: async () => "sent" }, {
    compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
    terminalPolicy,
    observeTerminal: async () => ({ agentSessionId: "wrong", executionId: "execution-1", observedAt: "2026-09-07T00:00:01.000Z", signal: TERMINAL_SIGNAL, finish: TERMINAL_OUTCOME }),
    testing: {
      fetchImpl: async () => { posts += 1; return new Response(JSON.stringify(admission())); },
      resolveProject: async () => ({ projectId: PROJECT_ID, gitRoot: "/repo" }),
      eventId: EVENT_ID,
    },
  });
  assert.equal(posts, 1);
  assert.equal(result.completion.attempted, false);
  const database = new DatabaseSync(join(dataDir, "adapter-terminal-outbox.db"), { readOnly: true });
  const row = database.prepare("SELECT state, envelope FROM terminal_outbox").get() as { state: string; envelope: string | null };
  database.close();
  assert.equal(row.state, "aborted");
  assert.equal(row.envelope, null);
});

// Reads the singleton capacity counter and asserts the O(1) invariant:
// the persisted counter always equals the unresolved reservation sum.
function unresolvedCharge(outbox: TerminalOutbox): number {
  const database = new DatabaseSync(outbox.databasePath, { readOnly: true });
  const counter = database
    .prepare("SELECT used_bytes FROM terminal_outbox_capacity WHERE id = 1")
    .get() as { used_bytes: number };
  const sum = database
    .prepare("SELECT COALESCE(SUM(reserved_bytes), 0) AS used FROM terminal_outbox WHERE state != 'terminal'")
    .get() as { used: number };
  database.close();
  assert.equal(counter.used_bytes, sum.used);
  return counter.used_bytes;
}

const completionAck = {
  status: "ok",
  data: { event: {
    event_id: COMPLETION_ID,
    status: "accepted",
    outcome: "completed",
    input_id: "input-1",
    task_id: "task-1",
    dispatch_authorized: false,
  } },
};

test("runtime sqlite driver selects node:sqlite on Node and drives the outbox", async (t) => {
  const driver = await loadSqliteDriver();
  assert.equal(driver.name, "node-sqlite");
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-driver-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  const outbox = await TerminalOutbox.open(policy(dataDir));
  try {
    assert.equal(outbox.driverName, "node-sqlite");
    assert.ok(outbox.reserve("session-1", "execution-1", TERMINAL_COMPATIBILITY_PROFILE));
    assert.equal(unresolvedCharge(outbox), 2048);
  } finally {
    outbox.close();
  }
});

test("production policy defaults match P14/E10 candidates and admit injected overrides", () => {
  const defaults = createTerminalOutboxPolicy();
  assert.equal(defaults.reservationBytes, 16_384);
  assert.equal(defaults.maxBytes, 201_326_592);
  assert.equal(defaults.authorizationWindowMs, 2_000);
  assert.equal(defaults.leaseMs, 5_000);
  assert.ok(Number.isFinite(defaults.clock()));

  const injected = createTerminalOutboxPolicy({
    reservationBytes: 2048,
    authorizationWindowMs: 60_000,
    clock: () => NOW,
    dataDir: "Z:/custom",
  });
  assert.equal(injected.reservationBytes, 2048);
  assert.equal(injected.authorizationWindowMs, 60_000);
  assert.equal(injected.maxBytes, 201_326_592);
  assert.equal(injected.clock(), NOW);
  assert.equal(injected.dataDir, "Z:/custom");
});

test("adapter fills production defaults from a partial policy and keeps terminal opt-in", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-defaults-"));
  const partialPolicy: Partial<TerminalOutboxPolicy> = { dataDir };
  t.after(async () => {
    await stopTerminalOutboxController(createTerminalOutboxPolicy(partialPolicy));
    rmSync(dataDir, { recursive: true, force: true });
  });

  let completionBody: Record<string, unknown> | undefined;
  let midFlight: { state: string; accounted_bytes: number; counter: number } | undefined;
  const fetchImpl: FetchImpl = async (_url, init) => {
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    if (body["event_type"] === "input_candidate") {
      return new Response(JSON.stringify(admission()));
    }
    if (body["event_type"] === "task_completed") {
      completionBody = body;
      const database = new DatabaseSync(join(dataDir, "adapter-terminal-outbox.db"), { readOnly: true });
      const row = database
        .prepare("SELECT state, accounted_bytes FROM terminal_outbox")
        .get() as { state: string; accounted_bytes: number };
      const counter = database
        .prepare("SELECT used_bytes FROM terminal_outbox_capacity WHERE id = 1")
        .get() as { used_bytes: number };
      database.close();
      midFlight = { ...row, counter: counter.used_bytes };
    }
    return new Response(JSON.stringify(completionAck));
  };

  const result = await dispatchOpenCodeV1(request(), { dispatch: async () => "sent" }, {
    compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
    terminalPolicy: partialPolicy,
    observeTerminal: async () => ({
      agentSessionId: "session-1",
      executionId: "execution-1",
      observedAt: "2026-09-07T00:00:01.000Z",
      signal: TERMINAL_SIGNAL,
      finish: TERMINAL_OUTCOME,
    }),
    testing: {
      fetchImpl,
      resolveProject: async () => ({ projectId: PROJECT_ID, gitRoot: "/repo" }),
      eventId: EVENT_ID,
      completionEventId: COMPLETION_ID,
    },
  });

  const completion = (result as unknown as { completion: Record<string, unknown> }).completion;
  assert.equal(completion["attempted"], true);
  assert.equal(completion["confirmed"], true);
  assert.equal(
    (completionBody!["payload"] as Record<string, unknown>)["capture_not_after"],
    "2026-09-07T00:00:03.000Z",
  );
  assert.equal(midFlight!.state, "leased");
  assert.equal(midFlight!.accounted_bytes, 16_384);
  assert.equal(midFlight!.counter, 16_384);
  const verifier = await TerminalOutbox.open(createTerminalOutboxPolicy(partialPolicy));
  assert.equal(unresolvedCharge(verifier), 0);
  verifier.close();
});

test("terminal disabled never loads the sqlite driver or creates the outbox database", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-disabled-"));
  const previous = process.env["CRUCIBLE_DATA_DIR"];
  process.env["CRUCIBLE_DATA_DIR"] = dataDir;
  t.after(() => {
    if (previous === undefined) delete process.env["CRUCIBLE_DATA_DIR"];
    else process.env["CRUCIBLE_DATA_DIR"] = previous;
    rmSync(dataDir, { recursive: true, force: true });
  });

  let posts = 0;
  const result = await dispatchOpenCodeV1(request(), { dispatch: async () => "sent" }, {
    compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
    observeTerminal: async () => ({
      agentSessionId: "session-1",
      executionId: "execution-1",
      observedAt: "2026-09-07T00:00:01.000Z",
      signal: TERMINAL_SIGNAL,
      finish: TERMINAL_OUTCOME,
    }),
    testing: {
      fetchImpl: async () => {
        posts += 1;
        return new Response(JSON.stringify(admission()));
      },
      resolveProject: async () => ({ projectId: PROJECT_ID, gitRoot: "/repo" }),
      eventId: EVENT_ID,
    },
  });

  assert.equal(posts, 1);
  assert.equal(existsSync(join(dataDir, "adapter-terminal-outbox.db")), false);
  const completion = (result as unknown as { completion: Record<string, unknown> }).completion;
  assert.equal(completion["attempted"], false);
  assert.equal(completion["reason"], "COMPATIBILITY_PROFILE_MISMATCH");
});

test("legacy outbox capacity initializes once from unresolved reserved_bytes", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-capacity-legacy-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  const legacy = new DatabaseSync(join(dataDir, "adapter-terminal-outbox.db"));
  legacy.exec(`
    CREATE TABLE terminal_outbox (
      id TEXT PRIMARY KEY,
      state TEXT NOT NULL,
      reserved_bytes INTEGER NOT NULL,
      accounted_bytes INTEGER NOT NULL,
      created_at INTEGER NOT NULL,
      agent_session_id TEXT NOT NULL,
      execution_id TEXT NOT NULL,
      compatibility_profile TEXT NOT NULL,
      task_id TEXT,
      input_id TEXT,
      event_id TEXT UNIQUE,
      envelope TEXT,
      payload_hash TEXT,
      attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt_at INTEGER,
      lease_until INTEGER,
      last_diagnostic TEXT
    ) STRICT;
  `);
  const insertLegacy = legacy.prepare(`
    INSERT INTO terminal_outbox
      (id, state, reserved_bytes, accounted_bytes, created_at,
       agent_session_id, execution_id, compatibility_profile, task_id, input_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `);
  insertLegacy.run("legacy-admitted", "admitted", 2048, 300, 1, "session-1", "execution-1",
    TERMINAL_COMPATIBILITY_PROFILE, "task-1", "input-1");
  insertLegacy.run("legacy-ready", "ready", 2048, 500, 2, "session-2", "execution-2",
    TERMINAL_COMPATIBILITY_PROFILE, "task-2", "input-2");
  legacy.close();

  const outbox = await TerminalOutbox.open(policy(dataDir, { maxBytes: 4096 }));
  try {
    assert.equal(unresolvedCharge(outbox), 4096);
    assert.equal(outbox.reserve("new", "new", TERMINAL_COMPATIBILITY_PROFILE), null);

    outbox.storeCompletion("legacy-admitted", completionInput());
    const database = new DatabaseSync(outbox.databasePath, { readOnly: true });
    const ready = database
      .prepare("SELECT state, accounted_bytes FROM terminal_outbox WHERE id = 'legacy-admitted'")
      .get() as { state: string; accounted_bytes: number };
    database.close();
    assert.equal(ready.state, "ready");
    assert.equal(ready.accounted_bytes, 2048);
    assert.equal(unresolvedCharge(outbox), 4096);

    const delivery = await outbox.deliver({ ...CORE_OPTIONS,
      fetchImpl: async () => new Response(JSON.stringify(completionAck)),
    }, "legacy-admitted");
    assert.equal(delivery?.terminal, true);
    assert.equal(unresolvedCharge(outbox), 2048);
    assert.ok(outbox.reserve("new", "new", TERMINAL_COMPATIBILITY_PROFILE));
  } finally {
    outbox.close();
  }
});

test("fixed reservation charge is stable across every unresolved state and released once", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-charge-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  const outbox = await TerminalOutbox.open(policy(dataDir, { maxBytes: 4096 }));
  try {
    const reserved = outbox.reserve("session-1", "execution-1", TERMINAL_COMPATIBILITY_PROFILE)!;
    assert.equal(unresolvedCharge(outbox), 2048);
    outbox.bindAdmission(reserved, "task-1", "input-1");
    assert.equal(unresolvedCharge(outbox), 2048);
    const stored = outbox.storeCompletion(reserved, completionInput());
    assert.ok(stored.bytes < 2048);
    assert.equal(unresolvedCharge(outbox), 2048);

    let leasedCharge: number | undefined;
    await outbox.deliver({ ...CORE_OPTIONS, fetchImpl: async () => {
      leasedCharge = unresolvedCharge(outbox);
      throw new Error("lost");
    } }, reserved);
    assert.equal(leasedCharge, 2048);
    assert.equal(unresolvedCharge(outbox), 2048);

    const delivered = await outbox.deliver({ ...CORE_OPTIONS,
      fetchImpl: async () => new Response(JSON.stringify(completionAck)),
    }, reserved);
    assert.equal(delivered?.terminal, true);
    assert.equal(delivered?.accepted, true);
    assert.equal(unresolvedCharge(outbox), 0);

    const repeat = await outbox.deliver({ ...CORE_OPTIONS,
      fetchImpl: async () => {
        throw new Error("unexpected second ack");
      },
    }, reserved);
    assert.equal(repeat, undefined);
    assert.equal(unresolvedCharge(outbox), 0);
  } finally {
    outbox.close();
  }
});

test("aborted obligations keep the fixed charge until a correlated abort ack", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-charge-abort-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  const outbox = await TerminalOutbox.open(policy(dataDir, { maxBytes: 4096 }));
  try {
    const candidate = createCanonicalCandidate({
      agentSessionId: "session-1", messageId: "input-1", executionId: "execution-1",
      projectId: PROJECT_ID, gitRoot: "/repo", workspacePath: "/repo", delivery: "new",
    }, { adapter: "opencode-v1", adapterVersion: "0.1.0", eventId: EVENT_ID });
    const reservation = outbox.reserveCandidate({ candidate, agentSessionId: "session-1",
      executionId: "execution-1", projectId: PROJECT_ID, gitRoot: "/repo", workspacePath: "/repo",
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE, reconciliationDelayMs: 2000 })!;
    outbox.bindAdmission(reservation, "task-1", "input-row-1");
    assert.equal(unresolvedCharge(outbox), 2048);
    outbox.markAdmissionAborted("session-1", "execution-1", "TERMINAL_SIGNAL_MISMATCH");
    assert.equal(unresolvedCharge(outbox), 2048);

    const abortDelivery = await outbox.deliver({ ...CORE_OPTIONS, fetchImpl: async (_url, init) => {
      const posted = JSON.parse(String(init?.body)) as Record<string, unknown>;
      return new Response(JSON.stringify({ status: "ok", data: { event: {
        event_id: posted["event_id"], status: "rejected", outcome: "rejected",
        input_id: "input-row-1", task_id: "task-1", dispatch_authorized: false,
      } } }));
    } }, reservation);
    assert.equal(abortDelivery?.terminal, true);
    assert.equal(abortDelivery?.accepted, false);
    assert.equal(unresolvedCharge(outbox), 0);
    assert.ok(outbox.reserve("new", "new", TERMINAL_COMPATIBILITY_PROFILE));
  } finally {
    outbox.close();
  }
});

test("capacity accounting is transactional across concurrent outbox connections", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-concurrent-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  const shared = { maxBytes: 6144, reservationBytes: 2048 };
  const a = await TerminalOutbox.open(policy(dataDir, shared));
  const b = await TerminalOutbox.open(policy(dataDir, shared));
  try {
    const first = a.reserve("s1", "e1", TERMINAL_COMPATIBILITY_PROFILE)!;
    assert.ok(b.reserve("s2", "e2", TERMINAL_COMPATIBILITY_PROFILE));
    assert.ok(a.reserve("s3", "e3", TERMINAL_COMPATIBILITY_PROFILE));
    assert.equal(unresolvedCharge(a), 6144);
    assert.equal(b.reserve("s4", "e4", TERMINAL_COMPATIBILITY_PROFILE), null);
    assert.equal(unresolvedCharge(b), 6144);

    a.cancelReservation(first);
    assert.equal(unresolvedCharge(a), 4096);
    assert.ok(b.reserve("s4", "e4", TERMINAL_COMPATIBILITY_PROFILE));
    assert.equal(unresolvedCharge(b), 6144);

    const c = await TerminalOutbox.open(policy(dataDir, shared));
    try {
      assert.equal(unresolvedCharge(c), 6144);
      assert.equal(c.reserve("s5", "e5", TERMINAL_COMPATIBILITY_PROFILE), null);
    } finally {
      c.close();
    }
  } finally {
    a.close();
    b.close();
  }
});

test("oversized candidate envelope is rejected before the candidate POST and fails open", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-oversized-"));
  const terminalPolicy = policy(dataDir, { maxBytes: 4096 });
  t.after(async () => {
    await stopTerminalOutboxController(terminalPolicy);
    rmSync(dataDir, { recursive: true, force: true });
  });

  let candidatePosts = 0;
  const fetchImpl: FetchImpl = async (_url, init) => {
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    if (body["event_type"] === "input_candidate") candidatePosts += 1;
    return new Response(JSON.stringify(admission()));
  };
  const result = await dispatchOpenCodeV1({ ...request(), prompt: "x".repeat(4096) },
    { dispatch: async () => "sent" }, {
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      terminalPolicy,
      observeTerminal: async () => null,
      testing: {
        fetchImpl,
        resolveProject: async () => ({ projectId: PROJECT_ID, gitRoot: "/repo" }),
        eventId: EVENT_ID,
      },
    });

  assert.equal(candidatePosts, 0);
  assert.equal(result.dispatchResult, "sent");
  assert.equal(result.tracked, false);
  assert.equal((result as { diagnostic?: string }).diagnostic, "TERMINAL_CANDIDATE_OVERSIZED");

  const outbox = await TerminalOutbox.open(terminalPolicy);
  assert.equal(unresolvedCharge(outbox), 0);
  assert.ok(outbox.reserve("new", "new", TERMINAL_COMPATIBILITY_PROFILE));
  outbox.close();
});

test("controller exposes the compatible TerminalOutbox facade and timers stay out of the store", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-outbox-facade-compat-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  const controllerPolicy = policy(dataDir);
  const controller = await TerminalOutboxController.open(controllerPolicy, { ...CORE_OPTIONS,
    fetchImpl: async () => { throw new Error("no core"); },
  });
  try {
    assert.ok(controller.outbox instanceof TerminalOutbox);
    assert.equal(typeof controller.outbox.deliver, "function");
    assert.equal(typeof controller.outbox.deliverDue, "function");
    assert.equal(controller.reservationBytes, controller.outbox.reservationBytes);
    // Boundary: delivery internals live on the store, never on the facade.
    // A public `extends TerminalOutboxStore` would leak them via the
    // prototype into the .d.ts; private composition keeps them out while
    // deliver/deliverDue keep working through the internal store.
    for (const internal of [
      "releaseCandidate",
      "retryCandidate",
      "finishDelivery",
      "retryDelivery",
      "leaseDelivery",
      "listDueCandidates",
      "listDueDeliveryIds",
    ]) {
      assert.equal(internal in controller.outbox, false, `${internal} leaks onto TerminalOutbox`);
      assert.equal(
        internal in TerminalOutbox.prototype,
        false,
        `${internal} leaks onto TerminalOutbox.prototype`,
      );
    }
    // The preserved pre-R7 surface still delegates through the facade.
    for (const member of [
      "databasePath",
      "driverName",
      "reservationBytes",
      "close",
      "reserve",
      "reserveCandidate",
      "cancelReservation",
      "bindAdmission",
      "reconcileCandidateResponse",
      "markAborted",
      "storeCompletionForAdmission",
      "markAdmissionAborted",
      "storeCompletion",
      "nextWakeAt",
      "deliver",
      "deliverDue",
    ]) {
      assert.equal(member in controller.outbox, true, `${member} missing on TerminalOutbox`);
    }
  } finally {
    await controller.stop();
  }

  const storeModule = await import("../src/runtime/terminal-outbox/terminal-outbox-store.js");
  assert.equal("createTerminalOutboxPolicy" in storeModule, false);
  assert.equal(typeof createTerminalOutboxPolicy().timer.schedule, "function");
});
