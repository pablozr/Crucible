import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { type TestContext } from "node:test";

import {
  TERMINAL_COMPATIBILITY_PROFILE,
  TERMINAL_OUTCOME,
  TERMINAL_SIGNAL,
  dispatchOpenCodeV1,
  stopTerminalOutboxController,
  type TerminalOutboxPolicy,
} from "../src/opencode-v1/index.js";
import type { FetchImpl } from "../src/runtime/contracts.js";

const PROJECT_ID = "123e4567-e89b-42d3-a456-426614174000";
const EVENT_ID = "123e4567-e89b-42d3-a456-426614174001";
const COMPLETION_EVENT_ID = "123e4567-e89b-42d3-a456-426614174002";
const OBSERVED_AT = "2026-09-07T00:00:00.000Z";

const uuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function request(overrides = {}) {
  return {
    openCodeVersion: "1.18.28",
    agentSessionId: "ses_123",
    messageId: "msg_456",
    executionId: "msg_456",
    workspacePath: "/repo",
    delivery: "new" as const,
    ...overrides,
  };
}

function stubResolve() {
  return async () => ({ projectId: PROJECT_ID, gitRoot: "/repo" });
}

function admissionEvent(overrides = {}) {
  return {
    status: "ok",
    message: "Event received.",
    data: {
      event: {
        event_id: EVENT_ID,
        status: "accepted",
        outcome: "admitted",
        input_id: "input-row-1",
        task_id: "task-1",
        dispatch_authorized: true,
        ...overrides,
      },
    },
  };
}

function completionEvent(eventId: string, taskId: string, overrides = {}) {
  return {
    status: "ok",
    message: "Event received.",
    data: {
      event: {
        event_id: eventId,
        status: "accepted",
        outcome: "completed",
        input_id: "input-row-1",
        task_id: taskId,
        dispatch_authorized: false,
        ...overrides,
      },
    },
  };
}

function exactObserver() {
  return async () => ({
    agentSessionId: "ses_123",
    executionId: "msg_456",
    observedAt: OBSERVED_AT,
    signal: TERMINAL_SIGNAL,
    finish: TERMINAL_OUTCOME,
  });
}

function terminalPolicy(t: TestContext): TerminalOutboxPolicy {
  const dataDir = mkdtempSync(join(tmpdir(), "crucible-terminal-completion-"));
  const policy: TerminalOutboxPolicy = {
    maxBytes: 4096,
    reservationBytes: 2048,
    authorizationWindowMs: 60_000,
    leaseMs: 1000,
    backoffBaseMs: 10,
    backoffMaxMs: 100,
    clock: () => Date.parse(OBSERVED_AT),
    jitter: () => 0,
    timer: {
      schedule: (callback, delayMs) => setTimeout(callback, delayMs),
      cancel: (handle) => clearTimeout(handle as NodeJS.Timeout),
    },
    busyTimeoutMs: 1000,
    dataDir,
  };
  t.after(async () => {
    await stopTerminalOutboxController(policy);
    rmSync(dataDir, { recursive: true, force: true });
  });
  return policy;
}

function dispatchStub<T = string>(impl?: (ctx: unknown) => T) {
  const calls: unknown[] = [];
  const fn = async (ctx: unknown) => {
    calls.push(ctx);
    if (impl) {
      return impl(ctx) as T;
    }
    return "sent" as T;
  };
  return { fn, calls };
}

function dualFetch(
  admissionBody: unknown,
  completionBody: unknown | ((body: Record<string, unknown>) => unknown),
  seen: Array<Record<string, unknown>> = [],
) {
  const fetchImpl: FetchImpl = async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    seen.push(body);

    if (body["event_type"] === "input_candidate") {
      return new Response(JSON.stringify(admissionBody), { status: 200 });
    }

    const resolved =
      typeof completionBody === "function"
        ? completionBody(body)
        : completionBody;
    return new Response(JSON.stringify(resolved), { status: 200 });
  };

  return { fetchImpl, seen };
}

test("durable completion is stored before posting after dispatch and observation", async (t) => {
  const order: string[] = [];
  const seen: Array<Record<string, unknown>> = [];
  const { fetchImpl } = dualFetch(
    admissionEvent(),
    completionEvent(COMPLETION_EVENT_ID, "task-1"),
    seen,
  );

  const gatedFetch: FetchImpl = async (url: string, init?: RequestInit) => {
    const preview = JSON.parse(String(init?.body)) as Record<string, unknown>;
    order.push(
      preview["event_type"] === "input_candidate"
        ? "fetch-candidate"
        : "fetch-completion",
    );
    return fetchImpl(url, init);
  };

  const { fn, calls } = dispatchStub((() => {
    order.push("dispatch");
    return "sent";
  }) as (ctx: unknown) => string);

  let observerCalls = 0;
  const result = await dispatchOpenCodeV1(
    request(),
    { dispatch: fn },
    {
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      terminalPolicy: terminalPolicy(t),
      observeTerminal: async () => {
        observerCalls += 1;
        order.push("observer");
        return { agentSessionId: "ses_123", executionId: "msg_456", observedAt: "2026-09-07T00:00:00.000Z", signal: "session_prompt_return", finish: "stop" };
      },
      testing: {
        fetchImpl: gatedFetch,
        resolveProject: stubResolve(),
        eventId: EVENT_ID,
        completionEventId: COMPLETION_EVENT_ID,
      },
    },
  );

  assert.equal(calls.length, 1);
  assert.equal(observerCalls, 1);
  assert.deepEqual(order, [
    "fetch-candidate",
    "dispatch",
    "observer",
    "fetch-completion",
  ]);
  assert.equal(seen.length, 2);
  assert.equal(result.tracked, true);
  assert.equal(result.dispatchResult, "sent");

  const completion = (result as unknown as { completion: Record<string, unknown> })
    .completion;
  assert.equal(completion["attempted"], true);
  assert.equal(completion["confirmed"], true);
  assert.equal(completion["eventId"], COMPLETION_EVENT_ID);
  assert.equal(completion["taskId"], "task-1");
});

test("durable completion payload contains exact observation authorization window", async (t) => {
  const seen: Array<Record<string, unknown>> = [];
  const { fetchImpl } = dualFetch(
    admissionEvent(),
    completionEvent(COMPLETION_EVENT_ID, "task-1"),
    seen,
  );
  const { fn } = dispatchStub();

  await dispatchOpenCodeV1(
    request(),
    { dispatch: fn },
    {
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      terminalPolicy: terminalPolicy(t),
      observeTerminal: exactObserver(),
      testing: {
        fetchImpl,
        resolveProject: stubResolve(),
        eventId: EVENT_ID,
        completionEventId: COMPLETION_EVENT_ID,
      },
    },
  );

  assert.equal(seen.length, 2);
  const completion = seen[1] as Record<string, unknown>;

  assert.equal(completion["event_type"], "task_completed");
  assert.equal(completion["event_id"], COMPLETION_EVENT_ID);
  assert.equal(completion["payload_version"], 1);
  assert.equal(completion["adapter"], "opencode-v1");
  assert.equal(completion["adapter_version"], "0.1.0");
  assert.equal(completion["agent_session_id"], "ses_123");
  assert.equal(completion["input_id"], "msg_456");
  assert.equal(completion["execution_id"], "msg_456");
  assert.equal(completion["project_id"], PROJECT_ID);
  assert.equal(completion["git_root"], "/repo");
  assert.equal(completion["workspace_path"], "/repo");
  assert.deepEqual(completion["payload"], {
    task_id: "task-1",
    terminal_signal: "session_prompt_return",
    terminal_outcome: "stop",
    compatibility_profile: TERMINAL_COMPATIBILITY_PROFILE,
    terminal_observed_at: OBSERVED_AT,
    capture_not_after: "2026-09-07T00:01:00.000Z",
  });
});

test("durable completion uses an independent generated event identity", async (t) => {
  const seen: Array<Record<string, unknown>> = [];
  const fetchImpl: FetchImpl = async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    seen.push(body);

    if (body["event_type"] === "input_candidate") {
      return new Response(
        JSON.stringify(admissionEvent({ event_id: body["event_id"] })),
        { status: 200 },
      );
    }

    const eventId = String(body["event_id"]);
    return new Response(
      JSON.stringify(completionEvent(eventId, "task-1")),
      { status: 200 },
    );
  };
  const { fn } = dispatchStub();

  const result = await dispatchOpenCodeV1(
    request(),
    { dispatch: fn },
    {
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      terminalPolicy: terminalPolicy(t),
      observeTerminal: exactObserver(),
      testing: { fetchImpl, resolveProject: stubResolve() },
    },
  );

  assert.equal(seen.length, 2);
  const admissionId = String(seen[0]?.["event_id"]);
  const completionId = String(seen[1]?.["event_id"]);

  assert.match(admissionId, uuidPattern);
  assert.match(completionId, uuidPattern);
  assert.notEqual(completionId, admissionId);
  assert.notEqual(completionId, "msg_456");
  assert.notEqual(admissionId, "msg_456");

  const completion = (result as unknown as { completion: Record<string, unknown> })
    .completion;
  assert.equal(completion["attempted"], true);
  assert.equal(completion["confirmed"], true);
  assert.equal(completion["eventId"], completionId);
});

test("ordinary dispatch without profile or observer posts no completion", async () => {
  const seen: Array<Record<string, unknown>> = [];
  const { fetchImpl } = dualFetch(
    admissionEvent(),
    completionEvent(COMPLETION_EVENT_ID, "task-1"),
    seen,
  );
  const { fn, calls } = dispatchStub();

  const result = await dispatchOpenCodeV1(
    request({ prompt: "do it" }),
    { dispatch: fn },
    {
      testing: {
        fetchImpl,
        resolveProject: stubResolve(),
        eventId: EVENT_ID,
      },
    },
  );

  assert.equal(seen.length, 1);
  assert.equal(seen[0]?.["event_type"], "input_candidate");
  assert.equal(calls.length, 1);
  assert.equal(result.tracked, true);

  const completion = (result as unknown as { completion: Record<string, unknown> })
    .completion;
  assert.equal(completion["attempted"], false);
  assert.equal(completion["reason"], "COMPATIBILITY_PROFILE_MISMATCH");
});

test("blank execution id is rejected before terminal reservation or Core admission", async (t) => {
  const policy = terminalPolicy(t);
  let fetchCalls = 0;
  const { fn, calls } = dispatchStub();
  const result = await dispatchOpenCodeV1(
    request({ executionId: "   " }),
    { dispatch: fn },
    {
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      terminalPolicy: policy,
      observeTerminal: exactObserver(),
      testing: {
        fetchImpl: async () => {
          fetchCalls += 1;
          throw new Error("unexpected Core call");
        },
        resolveProject: stubResolve(),
        eventId: EVENT_ID,
      },
    },
  );

  assert.equal(fetchCalls, 0);
  assert.equal(calls.length, 1);
  assert.equal(result.tracked, false);
  assert.equal((calls[0] as { diagnostic: string }).diagnostic, "INVALID_DISPATCH_REQUEST");
});

test("ordinary dispatch preserves compatibility when execution id is empty", async () => {
  const seen: Array<Record<string, unknown>> = [];
  const { fetchImpl } = dualFetch(admissionEvent(), {}, seen);
  const result = await dispatchOpenCodeV1(
    request({ executionId: "" }),
    { dispatch: async () => "sent" },
    { testing: { fetchImpl, resolveProject: stubResolve(), eventId: EVENT_ID } },
  );

  assert.equal(result.tracked, true);
  assert.equal(seen.length, 1);
  assert.equal("execution_id" in seen[0]!, false);
});

test("wrong compatibility profile posts no completion and skips observer", async () => {
  const seen: Array<Record<string, unknown>> = [];
  const { fetchImpl } = dualFetch(
    admissionEvent(),
    completionEvent(COMPLETION_EVENT_ID, "task-1"),
    seen,
  );
  const { fn } = dispatchStub();

  let observerCalls = 0;
  const result = await dispatchOpenCodeV1(
    request(),
    { dispatch: fn },
    {
      compatibilityProfile: "opencode-v1-unrestricted",
      observeTerminal: async () => {
        observerCalls += 1;
        return { agentSessionId: "ses_123", executionId: "msg_456", observedAt: "2026-09-07T00:00:00.000Z", signal: TERMINAL_SIGNAL, finish: TERMINAL_OUTCOME };
      },
      testing: {
        fetchImpl,
        resolveProject: stubResolve(),
        eventId: EVENT_ID,
      },
    },
  );

  assert.equal(seen.length, 1);
  assert.equal(observerCalls, 0);

  const completion = (result as unknown as { completion: Record<string, unknown> })
    .completion;
  assert.equal(completion["attempted"], false);
  assert.equal(completion["reason"], "COMPATIBILITY_PROFILE_MISMATCH");
});

test("durable policy without observer dispatches untracked without admission or reservation", async (t) => {
  const seen: Array<Record<string, unknown>> = [];
  const { fetchImpl } = dualFetch(
    admissionEvent(),
    completionEvent(COMPLETION_EVENT_ID, "task-1"),
    seen,
  );
  const { fn } = dispatchStub();

  const result = await dispatchOpenCodeV1(
    request(),
    { dispatch: fn },
    {
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      terminalPolicy: terminalPolicy(t),
      testing: {
        fetchImpl,
        resolveProject: stubResolve(),
        eventId: EVENT_ID,
      },
    },
  );

  assert.equal(seen.length, 0);
  assert.equal(result.tracked, false);

  const completion = (result as unknown as { completion: Record<string, unknown> })
    .completion;
  assert.equal(completion["attempted"], false);
  assert.equal(completion["reason"], "TERMINAL_SIGNAL_MISMATCH");
});

test("terminal controller filesystem failure dispatches untracked exactly once", async () => {
  const { fn, calls } = dispatchStub();
  const result = await dispatchOpenCodeV1(request(), { dispatch: fn }, {
    coreUrl: "http://core.test",
    timeoutMs: 2000,
    compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
    terminalPolicy: {
      maxBytes: 4096, reservationBytes: 2048, authorizationWindowMs: 60_000,
      leaseMs: 1000, backoffBaseMs: 10, backoffMaxMs: 100, busyTimeoutMs: 1000,
      clock: () => Date.now(), jitter: () => 0,
      timer: { schedule: () => undefined, cancel: () => {} },
      dataDir: "\0invalid",
    },
    observeTerminal: exactObserver(),
    testing: { fetchImpl: async () => { throw new Error("must not post"); }, resolveProject: stubResolve(), eventId: EVENT_ID },
  });
  assert.equal(calls.length, 1);
  assert.equal(result.dispatchResult, "sent");
  assert.equal(result.tracked, false);
  assert.equal((calls[0] as { diagnostic: string }).diagnostic, "TERMINAL_CONTROLLER_UNAVAILABLE");
});

test("missing production Core configuration fails open without network defaults", async () => {
  const { fn, calls } = dispatchStub();
  const result = await dispatchOpenCodeV1(request(), { dispatch: fn });
  assert.equal(calls.length, 1);
  assert.equal(result.tracked, false);
  assert.equal((calls[0] as { diagnostic: string }).diagnostic, "CORE_CONFIGURATION_REQUIRED");
});

test("durable policy rejects non-exact terminal observations", async (t) => {
  const cases: Array<{
    name: string;
    observation: { agentSessionId: string; executionId: string; observedAt: string; signal: string; finish: string } | null;
  }> = [
    { name: "idle", observation: { agentSessionId: "ses_123", executionId: "msg_456", observedAt: "2026-09-07T00:00:00.000Z", signal: "session_idle", finish: "stop" } },
    {
      name: "accepted",
      observation: { agentSessionId: "ses_123", executionId: "msg_456", observedAt: "2026-09-07T00:00:00.000Z", signal: "message_accepted", finish: "stop" },
    },
    {
      name: "wrong finish",
      observation: { agentSessionId: "ses_123", executionId: "msg_456", observedAt: "2026-09-07T00:00:00.000Z", signal: "session_prompt_return", finish: "error" },
    },
    { name: "null", observation: null },
  ];

  for (const item of cases) {
    const seen: Array<Record<string, unknown>> = [];
    const { fetchImpl } = dualFetch(
      admissionEvent(),
      completionEvent(COMPLETION_EVENT_ID, "task-1"),
      seen,
    );
    const { fn } = dispatchStub();

    const result = await dispatchOpenCodeV1(
      request(),
      { dispatch: fn },
      {
        compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
        terminalPolicy: terminalPolicy(t),
        observeTerminal: async () => item.observation,
        testing: {
          fetchImpl,
          resolveProject: stubResolve(),
          eventId: EVENT_ID,
        },
      },
    );

    assert.equal(seen.length, 1, item.name);
    const completion = (result as unknown as { completion: Record<string, unknown> })
      .completion;
    assert.equal(completion["attempted"], false, item.name);
    assert.equal(completion["reason"], "TERMINAL_SIGNAL_MISMATCH", item.name);
  }
});

test("durable admission survives observer failure without completion post", async (t) => {
  const seen: Array<Record<string, unknown>> = [];
  const { fetchImpl } = dualFetch(
    admissionEvent(),
    completionEvent(COMPLETION_EVENT_ID, "task-1"),
    seen,
  );
  const { fn } = dispatchStub();

  const result = await dispatchOpenCodeV1(
    request(),
    { dispatch: fn },
    {
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      terminalPolicy: terminalPolicy(t),
      observeTerminal: async () => {
        throw new Error("OBSERVER_DOWN");
      },
      testing: {
        fetchImpl,
        resolveProject: stubResolve(),
        eventId: EVENT_ID,
      },
    },
  );

  assert.equal(seen.length, 1);
  assert.equal(result.dispatchResult, "sent");

  const completion = (result as unknown as { completion: Record<string, unknown> })
    .completion;
  assert.equal(completion["attempted"], false);
  assert.equal(completion["reason"], "TERMINAL_SIGNAL_MISMATCH");
});

test("incompatible version posts no completion even with profile and signal", async () => {
  let fetchCalls = 0;
  const fetchImpl: FetchImpl = async () => {
    fetchCalls += 1;
    return new Response(JSON.stringify(admissionEvent()), { status: 200 });
  };
  const { fn } = dispatchStub();

  let observerCalls = 0;
  const result = await dispatchOpenCodeV1(
    request({ openCodeVersion: "1.18.29" }),
    { dispatch: fn },
    {
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      observeTerminal: async () => {
        observerCalls += 1;
        return { agentSessionId: "ses_123", executionId: "msg_456", observedAt: "2026-09-07T00:00:00.000Z", signal: TERMINAL_SIGNAL, finish: TERMINAL_OUTCOME };
      },
      testing: { fetchImpl, resolveProject: stubResolve() },
    },
  );

  assert.equal(fetchCalls, 0);
  assert.equal(observerCalls, 0);
  assert.equal(result.tracked, false);

  const completion = (result as unknown as { completion: Record<string, unknown> })
    .completion;
  assert.equal(completion["attempted"], false);
});

test("untracked admission posts no completion", async () => {
  const seen: Array<Record<string, unknown>> = [];
  const fetchImpl: FetchImpl = async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    seen.push(body);
    throw new Error("boom");
  };
  const { fn } = dispatchStub();

  let observerCalls = 0;
  const result = await dispatchOpenCodeV1(
    request(),
    { dispatch: fn },
    {
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      observeTerminal: async () => {
        observerCalls += 1;
        return { agentSessionId: "ses_123", executionId: "msg_456", observedAt: "2026-09-07T00:00:00.000Z", signal: TERMINAL_SIGNAL, finish: TERMINAL_OUTCOME };
      },
      testing: { fetchImpl, resolveProject: stubResolve(), eventId: EVENT_ID },
    },
  );

  assert.equal(result.tracked, false);
  assert.equal(seen.length, 1);
  assert.equal(observerCalls, 0);

  const completion = (result as unknown as { completion: Record<string, unknown> })
    .completion;
  assert.equal(completion["attempted"], false);
  assert.equal(completion["reason"], "UNTRACKED_DISPATCH");
});

test("released overlap posts no completion", async () => {
  const seen: Array<Record<string, unknown>> = [];
  const { fetchImpl } = dualFetch(
    admissionEvent({
      outcome: "released_overlap",
      input_id: null,
      task_id: null,
      dispatch_authorized: false,
    }),
    completionEvent(COMPLETION_EVENT_ID, "task-1"),
    seen,
  );
  const { fn } = dispatchStub();

  const result = await dispatchOpenCodeV1(
    request(),
    { dispatch: fn },
    {
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      observeTerminal: exactObserver(),
      testing: {
        fetchImpl,
        resolveProject: stubResolve(),
        eventId: EVENT_ID,
      },
    },
  );

  assert.equal(result.tracked, false);
  assert.equal(seen.length, 1);

  const completion = (result as unknown as { completion: Record<string, unknown> })
    .completion;
  assert.equal(completion["attempted"], false);
});

test("dispatch throw propagates without completion post", async () => {
  const seen: Array<Record<string, unknown>> = [];
  const { fetchImpl } = dualFetch(
    admissionEvent(),
    completionEvent(COMPLETION_EVENT_ID, "task-1"),
    seen,
  );

  let observerCalls = 0;
  await assert.rejects(
    dispatchOpenCodeV1(
      request(),
      {
        dispatch: async () => {
          throw new Error("V1_DOWN");
        },
      },
      {
        compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
        observeTerminal: async () => {
          observerCalls += 1;
        return { agentSessionId: "ses_123", executionId: "msg_456", observedAt: "2026-09-07T00:00:00.000Z", signal: TERMINAL_SIGNAL, finish: TERMINAL_OUTCOME };
        },
        testing: {
          fetchImpl,
          resolveProject: stubResolve(),
          eventId: EVENT_ID,
        },
      },
    ),
    /V1_DOWN/,
  );

  assert.equal(seen.length, 1);
  assert.equal(observerCalls, 0);
});

test("durable completion remains unconfirmed after POST and reconciliation network failure", async (t) => {
  const seen: Array<Record<string, unknown>> = [];
  const fetchImpl: FetchImpl = async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    seen.push(body);

    if (body["event_type"] === "input_candidate") {
      return new Response(JSON.stringify(admissionEvent()), { status: 200 });
    }

    throw new Error("completion down");
  };
  const { fn } = dispatchStub();

  const result = await dispatchOpenCodeV1(
    request(),
    { dispatch: fn },
    {
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      terminalPolicy: terminalPolicy(t),
      observeTerminal: exactObserver(),
      testing: {
        fetchImpl,
        resolveProject: stubResolve(),
        eventId: EVENT_ID,
        completionEventId: COMPLETION_EVENT_ID,
      },
    },
  );

  assert.equal(seen.length, 2);
  assert.equal(result.dispatchResult, "sent");

  const completion = (result as unknown as { completion: Record<string, unknown> })
    .completion;
  assert.equal(completion["attempted"], true);
  assert.equal(completion["confirmed"], false);
  assert.equal(completion["eventId"], COMPLETION_EVENT_ID);
  assert.equal(completion["diagnostic"], "COMPLETION_UNCONFIRMED");
});

test("durable completion remains unconfirmed after malformed Core responses", async (t) => {
  const seen: Array<Record<string, unknown>> = [];
  const fetchImpl: FetchImpl = async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    seen.push(body);

    if (body["event_type"] === "input_candidate") {
      return new Response(JSON.stringify(admissionEvent()), { status: 200 });
    }

    return new Response("not-json", { status: 200 });
  };
  const { fn } = dispatchStub();

  const result = await dispatchOpenCodeV1(
    request(),
    { dispatch: fn },
    {
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      terminalPolicy: terminalPolicy(t),
      observeTerminal: exactObserver(),
      testing: {
        fetchImpl,
        resolveProject: stubResolve(),
        eventId: EVENT_ID,
        completionEventId: COMPLETION_EVENT_ID,
      },
    },
  );

  assert.equal(seen.length, 2);

  const completion = (result as unknown as { completion: Record<string, unknown> })
    .completion;
  assert.equal(completion["attempted"], true);
  assert.equal(completion["confirmed"], false);
  assert.equal(completion["diagnostic"], "COMPLETION_UNCONFIRMED");
});

test("durable completion rejects mismatched direct POST confirmations", async (t) => {
  const mismatches: Array<{ name: string; body: unknown }> = [
    {
      name: "event id mismatch",
      body: completionEvent("123e4567-e89b-42d3-a456-426614174099", "task-1"),
    },
    {
      name: "wrong outcome",
      body: completionEvent(COMPLETION_EVENT_ID, "task-1", {
        outcome: "admitted",
      }),
    },
    {
      name: "wrong task",
      body: completionEvent(COMPLETION_EVENT_ID, "task-other"),
    },
    {
      name: "missing event",
      body: { status: "ok", data: {} },
    },
  ];

  for (const item of mismatches) {
    const seen: Array<Record<string, unknown>> = [];
    const { fetchImpl } = dualFetch(admissionEvent(), item.body, seen);
    const { fn } = dispatchStub();

    const result = await dispatchOpenCodeV1(
      request(),
      { dispatch: fn },
      {
        compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
        terminalPolicy: terminalPolicy(t),
        observeTerminal: exactObserver(),
        testing: {
          fetchImpl,
          resolveProject: stubResolve(),
          eventId: EVENT_ID,
          completionEventId: COMPLETION_EVENT_ID,
        },
      },
    );

    assert.equal(seen.length, 2, item.name);
    assert.equal(result.dispatchResult, "sent", item.name);

    const completion = (result as unknown as { completion: Record<string, unknown> })
      .completion;
    assert.equal(completion["attempted"], true, item.name);
    assert.equal(completion["confirmed"], false, item.name);
    assert.equal(completion["eventId"], COMPLETION_EVENT_ID, item.name);
    assert.equal(
      completion["diagnostic"],
      "COMPLETION_UNCONFIRMED",
      item.name,
    );
  }
});

test("durable completion keeps Core errors retryable until terminal acknowledgement", async (t) => {
  const seen: Array<Record<string, unknown>> = [];
  const fetchImpl: FetchImpl = async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    seen.push(body);

    if (body["event_type"] === "input_candidate") {
      return new Response(JSON.stringify(admissionEvent()), { status: 200 });
    }

    return new Response(
      JSON.stringify({
        status: "error",
        message: "Terminal signal invalid.",
        data: { code: "INVALID_TERMINAL_SIGNAL" },
      }),
      { status: 400 },
    );
  };
  const { fn } = dispatchStub();

  const result = await dispatchOpenCodeV1(
    request(),
    { dispatch: fn },
    {
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      terminalPolicy: terminalPolicy(t),
      observeTerminal: exactObserver(),
      testing: {
        fetchImpl,
        resolveProject: stubResolve(),
        eventId: EVENT_ID,
        completionEventId: COMPLETION_EVENT_ID,
      },
    },
  );

  assert.equal(seen.length, 2);
  assert.equal(result.dispatchResult, "sent");

  const completion = (result as unknown as { completion: Record<string, unknown> })
    .completion;
  assert.equal(completion["attempted"], true);
  assert.equal(completion["confirmed"], false);
  assert.equal(completion["diagnostic"], "COMPLETION_UNCONFIRMED");
});
