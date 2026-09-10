import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { TERMINAL_COMPATIBILITY_PROFILE } from "../src/opencode-v1/index.js";
import {
  canonicalJson,
  legacyNormalizedEnvelopeHash,
  pydanticPosixPath,
  pydanticWindowsPath,
} from "../src/runtime/core-client.js";
import { TerminalOutbox, type TerminalOutboxPolicy } from "../src/runtime/terminal-outbox.js";

type Vector = {
  id: string;
  description: string;
  envelope: Record<string, unknown>;
  dumped: Record<string, unknown>;
  legacy_hash: string;
};

type Fixture = { version: number; cases: Vector[] };

function fixtureRoot(): string {
  return resolve(
    dirname(fileURLToPath(import.meta.url)),
    "..",
    "..",
    "..",
    "test",
    "fixtures",
    "legacy-envelope-hash",
  );
}

function loadFixture(name: string): Fixture {
  return JSON.parse(readFileSync(join(fixtureRoot(), name), "utf8")) as Fixture;
}

// The Core under test normalizes paths per its own host platform, so the
// adapter must reproduce the fixture of the platform it runs on.
function platformFixture(): { name: string; fixture: Fixture } {
  return process.platform === "win32"
    ? { name: "vectors.json", fixture: loadFixture("vectors.json") }
    : { name: "vectors-posix.json", fixture: loadFixture("vectors-posix.json") };
}

test("legacy fixtures are self-consistent Python truth", () => {
  for (const name of ["vectors.json", "vectors-posix.json"]) {
    const fixture = loadFixture(name);
    assert.equal(fixture.version, 1);
    assert.ok(fixture.cases.length > 0, name);
    for (const vector of fixture.cases) {
      assert.equal(
        createHash("sha256").update(canonicalJson(vector.dumped)).digest("hex"),
        vector.legacy_hash,
        `${name}/${vector.id}: fixture self-consistency`,
      );
    }
  }
});

test("windows path normalizer matches PureWindowsPath truth", () => {
  const fixture = loadFixture("vectors.json");
  assert.equal(fixture.cases.length, 9);
  for (const vector of fixture.cases) {
    assert.equal(
      pydanticWindowsPath(vector.envelope["git_root"] as string),
      vector.dumped["git_root"],
      `git_root/${vector.id}`,
    );
    assert.equal(
      pydanticWindowsPath(vector.envelope["workspace_path"] as string),
      vector.dumped["workspace_path"],
      `workspace_path/${vector.id}`,
    );
  }
});

test("posix path normalizer matches PurePosixPath truth", () => {
  const fixture = loadFixture("vectors-posix.json");
  assert.equal(fixture.cases.length, 11);
  for (const vector of fixture.cases) {
    assert.equal(
      pydanticPosixPath(vector.envelope["git_root"] as string),
      vector.dumped["git_root"],
      `git_root/${vector.id}`,
    );
    assert.equal(
      pydanticPosixPath(vector.envelope["workspace_path"] as string),
      vector.dumped["workspace_path"],
      `workspace_path/${vector.id}`,
    );
  }
});

test("legacy hashes match Pydantic truth on this platform", () => {
  const { name, fixture } = platformFixture();
  for (const vector of fixture.cases) {
    assert.equal(
      legacyNormalizedEnvelopeHash(canonicalJson(vector.envelope)),
      vector.legacy_hash,
      `${name}/${vector.id}`,
    );
  }
});

test("lexically distinct sent forms share one legacy hash when semantic", () => {
  for (const name of ["vectors.json", "vectors-posix.json"]) {
    const byId = new Map(loadFixture(name).cases.map((vector) => [vector.id, vector]));
    assert.equal(
      byId.get("trailing-separator")!.legacy_hash,
      byId.get("dot-segment")!.legacy_hash,
      `${name}: trailing/dot-segment`,
    );
    assert.equal(
      byId.get("plus-offset-zero")!.legacy_hash,
      byId.get("uppercase-uuids")!.legacy_hash,
      `${name}: offset-zero/uppercase-uuids`,
    );
  }
  const posix = new Map(loadFixture("vectors-posix.json").cases.map((vector) => [vector.id, vector]));
  assert.equal(
    posix.get("dot-segment")!.legacy_hash,
    posix.get("repeated-separators")!.legacy_hash,
    "vectors-posix.json: dot-segment/repeated-separators",
  );
  const windows = new Map(loadFixture("vectors.json").cases.map((vector) => [vector.id, vector]));
  assert.equal(
    windows.get("dot-segment")!.legacy_hash,
    windows.get("windows-separators")!.legacy_hash,
    "vectors.json: dot-segment/windows-separators",
  );
});

function policy(dataDir: string): TerminalOutboxPolicy {
  return {
    maxBytes: 4096,
    reservationBytes: 2048,
    authorizationWindowMs: 60_000,
    leaseMs: 1000,
    backoffBaseMs: 10,
    backoffMaxMs: 100,
    clock: () => Date.parse("2026-09-07T00:00:00.000Z"),
    jitter: () => 0,
    timer: {
      schedule: (callback, delayMs) => setTimeout(callback, delayMs),
      cancel: (handle) => clearTimeout(handle as NodeJS.Timeout),
    },
    busyTimeoutMs: 1000,
    dataDir,
  };
}

function reserveFromVector(outbox: TerminalOutbox, vector: Vector): string {
  const envelope = canonicalJson(vector.envelope);
  const transportHash = createHash("sha256").update(envelope).digest("hex");
  assert.notEqual(
    vector.legacy_hash,
    transportHash,
    `${vector.id}: legacy branch must differ from transport hash`,
  );
  return outbox.reserveCandidate({
    candidate: {
      eventId: vector.envelope["event_id"] as string,
      envelope,
      payloadHash: transportHash,
    },
    agentSessionId: "session-1",
    executionId: "execution-1",
    projectId: vector.envelope["project_id"] as string,
    gitRoot: vector.envelope["git_root"] as string,
    workspacePath: vector.envelope["workspace_path"] as string,
    compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
    reconciliationDelayMs: 0,
  })!;
}

function admittedGet(vector: Vector) {
  return new Response(
    JSON.stringify({
      status: "ok",
      data: {
        event: {
          event_id: vector.envelope["event_id"],
          status: "accepted",
          outcome: "admitted",
          input_id: "input-1",
          task_id: "task-1",
          dispatch_authorized: true,
          payload_hash: vector.legacy_hash,
        },
      },
    }),
  );
}

test("candidate GET reconciles the platform legacy hash", async (t) => {
  const { name, fixture } = platformFixture();
  const vector = fixture.cases.find((entry) => entry.id === "trailing-separator")!;

  const dataDir = mkdtempSync(join(tmpdir(), "crucible-fixture-legacy-candidate-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  const outbox = await TerminalOutbox.open(policy(dataDir));
  const reservation = reserveFromVector(outbox, vector);

  await outbox.deliverDue({
    coreUrl: "http://core.test",
    timeoutMs: 2000,
    fetchImpl: async () => admittedGet(vector),
  });

  const database = new DatabaseSync(outbox.databasePath, { readOnly: true });
  const row = database.prepare("SELECT state, task_id FROM terminal_outbox WHERE id = ?").get(reservation) as {
    state: string;
    task_id: string;
  };
  database.close();
  outbox.close();
  assert.equal(row.state, "admitted", name);
  assert.equal(row.task_id, "task-1");
});

test("posix platform reconciles posix legacy hashes end to end", async (t) => {
  const actualPlatform = process.platform;
  Object.defineProperty(process, "platform", { value: "linux", configurable: true });
  t.after(() => {
    Object.defineProperty(process, "platform", { value: actualPlatform, configurable: true });
  });

  const fixture = loadFixture("vectors-posix.json");
  for (const vector of fixture.cases) {
    assert.equal(
      legacyNormalizedEnvelopeHash(canonicalJson(vector.envelope)),
      vector.legacy_hash,
      `posix/${vector.id}`,
    );
  }

  const vector = fixture.cases.find((entry) => entry.id === "dot-segment")!;
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-fixture-legacy-posix-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  const outbox = await TerminalOutbox.open(policy(dataDir));
  const reservation = reserveFromVector(outbox, vector);

  await outbox.deliverDue({
    coreUrl: "http://core.test",
    timeoutMs: 2000,
    fetchImpl: async () => admittedGet(vector),
  });

  const database = new DatabaseSync(outbox.databasePath, { readOnly: true });
  const row = database.prepare("SELECT state, task_id FROM terminal_outbox WHERE id = ?").get(reservation) as {
    state: string;
    task_id: string;
  };
  database.close();
  outbox.close();
  assert.equal(row.state, "admitted");
  assert.equal(row.task_id, "task-1");
});

test("double-slash root reconciles the Python-oracle legacy hash", async (t) => {
  const actualPlatform = process.platform;
  Object.defineProperty(process, "platform", { value: "linux", configurable: true });
  t.after(() => {
    Object.defineProperty(process, "platform", { value: actualPlatform, configurable: true });
  });
  const fixture = loadFixture("vectors-posix.json");
  const vector = fixture.cases.find((entry) => entry.id === "double-slash-root")!;
  // Fixture dumped/hash is the Python PurePosixPath oracle — the adapter
  // helper is under test here, never the expected value.
  assert.equal(vector.dumped["git_root"], "//repo/sub");
  assert.equal(vector.dumped["workspace_path"], "//repo/sub");
  assert.equal(
    legacyNormalizedEnvelopeHash(canonicalJson(vector.envelope)),
    vector.legacy_hash,
  );
  const singleSlash = fixture.cases.find((entry) => entry.id === "trailing-separator")!;
  assert.notEqual(vector.legacy_hash, singleSlash.legacy_hash);

  const dataDir = mkdtempSync(join(tmpdir(), "crucible-fixture-legacy-double-slash-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  const outbox = await TerminalOutbox.open(policy(dataDir));
  const reservation = reserveFromVector(outbox, vector);

  await outbox.deliverDue({
    coreUrl: "http://core.test",
    timeoutMs: 2000,
    fetchImpl: async () => admittedGet(vector),
  });

  const database = new DatabaseSync(outbox.databasePath, { readOnly: true });
  const row = database.prepare("SELECT state, task_id FROM terminal_outbox WHERE id = ?").get(reservation) as {
    state: string;
    task_id: string;
  };
  database.close();
  outbox.close();
  assert.equal(row.state, "admitted");
  assert.equal(row.task_id, "task-1");
});

test("candidate GET with an arbitrary hash remains reserved", async (t) => {
  const { fixture } = platformFixture();
  const vector = fixture.cases.find((entry) => entry.id === "trailing-separator")!;

  const dataDir = mkdtempSync(join(tmpdir(), "crucible-fixture-legacy-mismatch-"));
  t.after(() => rmSync(dataDir, { recursive: true, force: true }));
  const outbox = await TerminalOutbox.open(policy(dataDir));
  const reservation = reserveFromVector(outbox, vector);
  void reservation;

  await outbox.deliverDue({
    coreUrl: "http://core.test",
    timeoutMs: 2000,
    fetchImpl: async () =>
      new Response(
        JSON.stringify({
          status: "ok",
          data: {
            event: {
              event_id: vector.envelope["event_id"],
              status: "accepted",
              outcome: "admitted",
              input_id: "input-1",
              task_id: "task-1",
              dispatch_authorized: true,
              payload_hash: "0".repeat(64),
            },
          },
        }),
      ),
  });

  const database = new DatabaseSync(outbox.databasePath, { readOnly: true });
  const row = database.prepare("SELECT state, accounted_bytes FROM terminal_outbox").get() as {
    state: string;
    accounted_bytes: number;
  };
  database.close();
  outbox.close();
  assert.equal(row.state, "candidate_in_flight");
  assert.equal(row.accounted_bytes, 2048);
});
