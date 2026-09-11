import assert from "node:assert/strict";
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { createCanonicalCandidate, postCanonicalCandidate } from "../src/runtime/core-client.js";
import { TerminalOutbox } from "../src/runtime/terminal-outbox.js";
import {
  ADAPTER,
  ADAPTER_VERSION,
  TERMINAL_COMPATIBILITY_PROFILE,
  TERMINAL_OUTCOME,
  TERMINAL_SIGNAL,
} from "../src/opencode-v1/contracts.js";

const STARTUP_TIMEOUT_MS = 30000;
const STEP_TIMEOUT_MS = 15000;

// Independent legacy oracle: the hash Core computed before transport-byte
// hashing, reproduced by Core's own Pydantic schema (never the TS helper).
const LEGACY_HASH_SCRIPT = [
  "import sys, json, hashlib",
  "from crucible_core.schemas.admissions import EventRequest",
  "envelope = json.loads(sys.stdin.read())",
  "dumped = EventRequest.model_validate(envelope).model_dump(mode='json', exclude_none=True)",
  "sys.stdout.write(hashlib.sha256(json.dumps(dumped, sort_keys=True, separators=(',', ':')).encode()).hexdigest())",
].join("\n");

function freePort(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => {
        if (address && typeof address === "object") resolvePort(address.port);
        else reject(new Error("NO_FREE_PORT"));
      });
    });
  });
}

async function waitForHealth(url: string, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(2000) });
      if (response.ok) return;
    } catch {
      await delay(250);
      continue;
    }
    await delay(250);
  }
  throw new Error(`STARTUP_TIMEOUT: ${url}`);
}

test(
  "core transport hash: reserve-before-POST, lost responses reconcile via GET payload_hash",
  { timeout: 180000 },
  async (t) => {
    const root = dirname(fileURLToPath(import.meta.url));
    const repoRoot = resolve(root, "..", "..", "..");
    const repo = mkdtempSync(join(tmpdir(), "crucible-hash-repo-"));
    const coreData = mkdtempSync(join(tmpdir(), "crucible-hash-core-"));
    const outboxData = mkdtempSync(join(tmpdir(), "crucible-hash-outbox-"));
    let core: ChildProcess | undefined;
    let outbox: TerminalOutbox | undefined;

    t.after(async () => {
      try {
        outbox?.close();
      } catch {}
      if (core && core.exitCode === null) {
        if (process.platform === "win32" && core.pid !== undefined) {
          try {
            execFileSync("taskkill", ["/PID", String(core.pid), "/T", "/F"], { stdio: "ignore" });
          } catch {}
        } else {
          try {
            core.kill("SIGTERM");
          } catch {}
        }
      }
      for (const directory of [repo, coreData, outboxData]) {
        rmSync(directory, { recursive: true, force: true });
      }
    });

    execFileSync("git", ["init", "--quiet", repo]);
    execFileSync("git", ["-C", repo, "config", "user.email", "hash@example.com"]);
    execFileSync("git", ["-C", repo, "config", "user.name", "Hash"]);
    const projectId = randomUUID();
    mkdirSync(join(repo, ".crucible"), { recursive: true });
    writeFileSync(join(repo, ".crucible", "project.json"), JSON.stringify({ project_id: projectId }));
    writeFileSync(join(repo, "tracked.txt"), "before\n");
    execFileSync("git", ["-C", repo, "add", "."]);
    execFileSync("git", ["-C", repo, "commit", "--quiet", "-m", "init"]);

    const corePort = await freePort();
    const coreUrl = `http://127.0.0.1:${corePort}`;
    const rawCorePython = process.env.CRUCIBLE_CORE_PYTHON;
    const corePython = rawCorePython !== undefined && rawCorePython.trim().length > 0 ? rawCorePython : "python";
    core = spawn(corePython, ["-m", "uvicorn", "crucible_core.main:app", "--host", "127.0.0.1", "--port", String(corePort)], {
      cwd: join(repoRoot, "core"),
      env: {
        ...process.env,
        CRUCIBLE_DATA_DIR: coreData,
        CRUCIBLE_TERMINAL_MAX_AUTH_WINDOW_SECONDS: "3000000000",
        PYTHONPATH: join(repoRoot, "core", "src"),
      },
      stdio: "ignore",
    });
    await waitForHealth(`${coreUrl}/v1/health`, STARTUP_TIMEOUT_MS);

    const pythonLegacyHash = (envelope: string): string =>
      execFileSync(corePython, ["-c", LEGACY_HASH_SCRIPT], {
        input: envelope,
        encoding: "utf8",
        timeout: STEP_TIMEOUT_MS,
        cwd: join(repoRoot, "core"),
        env: { ...process.env, PYTHONPATH: join(repoRoot, "core", "src") },
      }).trim();

    const fetchJson = async (url: string, init?: RequestInit) => {
      const response = await fetch(url, { ...init, signal: AbortSignal.timeout(STEP_TIMEOUT_MS) });
      return { response, body: (await response.json()) as Record<string, unknown> };
    };
    const eventOf = (body: Record<string, unknown>) =>
      (body["data"] as Record<string, unknown>)["event"] as Record<string, unknown>;

    // A POST wrapper that persists through the real Core and then loses
    // exactly the response, mirroring a crash between commit and ACK.
    const losingPost = (counter: { posts: number; lastStatus?: number; lastBody?: string }) => async (url: string, init?: RequestInit) => {
      counter.posts += 1;
      const real = await fetch(url, { ...init, signal: AbortSignal.timeout(STEP_TIMEOUT_MS) });
      counter.lastStatus = real.status;
      counter.lastBody = await real.text();
      throw new Error("SIMULATED_LOST_POST_RESPONSE");
    };

    const outboxRow = (id: string) => {
      const database = new DatabaseSync(outbox!.databasePath, { readOnly: true });
      const row = database.prepare("SELECT state, task_id, input_id, candidate_envelope FROM terminal_outbox WHERE id = ?").get(id) as {
        state: string;
        task_id: string | null;
        input_id: string | null;
        candidate_envelope: string | null;
      };
      database.close();
      return row;
    };

    outbox = await TerminalOutbox.open({
      maxBytes: 1_048_576,
      reservationBytes: 16_384,
      authorizationWindowMs: 60_000,
      leaseMs: 1000,
      backoffBaseMs: 10,
      backoffMaxMs: 100,
      clock: () => Date.now(),
      jitter: () => 0,
      timer: { schedule: (callback, delayMs) => setTimeout(callback, delayMs), cancel: (handle) => clearTimeout(handle as NodeJS.Timeout) },
      busyTimeoutMs: 5000,
      dataDir: outboxData,
    });

    // Flow A: transport hash. The reservation precedes any POST; the POST
    // persists but its response is lost; only deliverDue's GET binds.
    const candidate = createCanonicalCandidate(
      {
        agentSessionId: "session-1",
        messageId: "input-1",
        executionId: "execution-1",
        projectId,
        gitRoot: repo,
        workspacePath: repo,
        delivery: "new",
      },
      { adapter: ADAPTER, adapterVersion: ADAPTER_VERSION, eventId: randomUUID() },
    );
    assert.equal(createHash("sha256").update(candidate.envelope).digest("hex"), candidate.payloadHash);

    const reservation = outbox.reserveCandidate({
      candidate,
      agentSessionId: "session-1",
      executionId: "execution-1",
      projectId,
      gitRoot: repo,
      workspacePath: repo,
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      reconciliationDelayMs: 0,
    })!;
    const lostA = { posts: 0 };
    const lostAdmission = await postCanonicalCandidate(candidate, {
      fetchImpl: losingPost(lostA),
      coreUrl,
      timeoutMs: STEP_TIMEOUT_MS,
    });
    assert.equal(lostAdmission.tracked, false);
    outbox.reconcileCandidateResponse(reservation, lostAdmission);

    let gets = 0;
    await outbox.deliverDue({
      coreUrl,
      timeoutMs: STEP_TIMEOUT_MS,
      fetchImpl: async (url, init) => {
        gets += 1;
        return fetch(url, init);
      },
    });
    assert.equal(lostA.posts, 1);
    assert.equal(gets, 1);
    const bound = outboxRow(reservation);
    assert.equal(bound.state, "admitted");
    assert.ok(typeof bound.task_id === "string" && bound.task_id.length > 0);
    assert.ok(typeof bound.input_id === "string" && bound.input_id.length > 0);
    assert.equal(bound.candidate_envelope, candidate.envelope);

    const admittedLookup = await fetchJson(`${coreUrl}/v1/events/${candidate.eventId}`);
    assert.equal(eventOf(admittedLookup.body)["payload_hash"], candidate.payloadHash);

    // Flow B: legacy hash. While the task still runs, a steer carrying
    // the task's own execution joins it; after the lost POST the stored
    // transport hash is replaced with the independently computed Pydantic
    // legacy hash, so GET reconciliation must take the fallback branch.
    const legacyCandidate = createCanonicalCandidate(
      {
        agentSessionId: "session-1",
        messageId: "input-2",
        executionId: "execution-1",
        projectId,
        gitRoot: repo,
        workspacePath: repo,
        delivery: "steer",
      },
      { adapter: ADAPTER, adapterVersion: ADAPTER_VERSION, eventId: randomUUID() },
    );
    const legacyReservation = outbox.reserveCandidate({
      candidate: legacyCandidate,
      agentSessionId: "session-1",
      executionId: "execution-1",
      projectId,
      gitRoot: repo,
      workspacePath: repo,
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      reconciliationDelayMs: 0,
    })!;
    const lostB: { posts: number; lastStatus?: number; lastBody?: string } = { posts: 0 };
    const lostLegacyAdmission = await postCanonicalCandidate(legacyCandidate, {
      fetchImpl: losingPost(lostB),
      coreUrl,
      timeoutMs: STEP_TIMEOUT_MS,
    });
    assert.equal(lostLegacyAdmission.tracked, false);
    assert.equal(lostB.lastStatus, 200);
    outbox.reconcileCandidateResponse(legacyReservation, lostLegacyAdmission);

    const legacyHash = pythonLegacyHash(legacyCandidate.envelope);
    assert.match(legacyHash, /^[0-9a-f]{64}$/);
    assert.notEqual(legacyHash, legacyCandidate.payloadHash);
    const coreDatabase = new DatabaseSync(join(coreData, "crucible.db"));
    coreDatabase.exec("PRAGMA busy_timeout=5000");
    const updated = coreDatabase
      .prepare("UPDATE inbound_events SET payload_hash = ? WHERE id = ?")
      .run(legacyHash, legacyCandidate.eventId);
    coreDatabase.close();
    assert.equal(updated.changes, 1);

    const legacyLookup = await fetchJson(`${coreUrl}/v1/events/${legacyCandidate.eventId}`);
    assert.equal(eventOf(legacyLookup.body)["payload_hash"], legacyHash);

    await outbox.deliverDue({
      coreUrl,
      timeoutMs: STEP_TIMEOUT_MS,
      fetchImpl: async (url, init) => fetch(url, init),
    });
    const legacyBound = outboxRow(legacyReservation);
    assert.equal(legacyBound.state, "admitted");
    assert.ok(typeof legacyBound.task_id === "string" && legacyBound.task_id.length > 0);
    assert.equal(legacyBound.task_id, bound.task_id);
    assert.equal(legacyBound.candidate_envelope, legacyCandidate.envelope);

    // Flow C: terminal for flow A. The explicit reservation id is used
    // (not the session/execution lookup) so the legacy steer row cannot
    // divert the obligation. The completion POST persists but its response
    // is lost; the same deliver GET reconciles via the exact-hash comparison.
    const observedAt = new Date().toISOString();
    const stored = outbox.storeCompletion(reservation, {
      eventId: randomUUID(),
      occurredAt: observedAt,
      adapter: ADAPTER,
      adapterVersion: ADAPTER_VERSION,
      agentSessionId: "session-1",
      inputId: "input-1",
      executionId: "execution-1",
      projectId,
      gitRoot: repo,
      workspacePath: repo,
      taskId: bound.task_id!,
      terminalSignal: TERMINAL_SIGNAL,
      terminalOutcome: TERMINAL_OUTCOME,
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      terminalObservedAt: observedAt,
    });
    assert.match(stored.payloadHash, /^[0-9a-f]{64}$/);

    const delivery = await outbox.deliver(
      {
        coreUrl,
        timeoutMs: STEP_TIMEOUT_MS,
        fetchImpl: async (url, init) => {
          if (init?.method === "POST") {
            const real = await fetch(url, { ...init, signal: AbortSignal.timeout(STEP_TIMEOUT_MS) });
            await real.text();
            throw new Error("SIMULATED_LOST_TERMINAL_RESPONSE");
          }
          return fetch(url, init);
        },
      },
      reservation,
    );
    assert.equal(delivery?.terminal, true);
    assert.equal(delivery?.accepted, true);

    const terminalLookup = await fetchJson(`${coreUrl}/v1/events/${stored.eventId}`);
    assert.equal(eventOf(terminalLookup.body)["payload_hash"], stored.payloadHash);
  },
);
