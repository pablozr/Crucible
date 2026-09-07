import assert from "node:assert/strict";
import test from "node:test";

import {
  postInputCandidate,
  type FetchImpl,
} from "../src/runtime/core-client.js";

const ADAPTER = "opencode-v1";
const ADAPTER_VERSION = "0.1.0";
const EVENT_ID = "123e4567-e89b-42d3-a456-426614174001";

function input(overrides = {}) {
  return {
    agentSessionId: "ses_123",
    messageId: "msg_456",
    workspacePath: "/repo",
    gitRoot: "/repo",
    projectId: "123e4567-e89b-42d3-a456-426614174000",
    delivery: "new" as const,
    ...overrides,
  };
}

function options(overrides = {}) {
  return {
    fetchImpl: async () =>
      new Response(JSON.stringify({ status: "ok", data: {} }), {
        status: 200,
      }),
    adapter: ADAPTER,
    adapterVersion: ADAPTER_VERSION,
    eventId: EVENT_ID,
    coreUrl: "http://core.test",
    timeoutMs: 2000,
    ...overrides,
  };
}

function okEvent(overrides = {}) {
  return {
    status: "ok",
    message: "Event received.",
    data: {
      event: {
        event_id: EVENT_ID,
        status: "accepted",
        outcome: "admitted",
        input_id: "msg_456",
        task_id: "task-1",
        dispatch_authorized: true,
        ...overrides,
      },
    },
  };
}

function okFetch(body: unknown) {
  let calls = 0;
  let lastBody: unknown;
  const fetchImpl: FetchImpl = async (_url: string, init?: RequestInit) => {
    calls += 1;
    lastBody = JSON.parse(String(init?.body));
    return new Response(JSON.stringify(body), { status: 200 });
  };
  return { fetchImpl, calls: () => calls, lastBody: () => lastBody };
}

test("admitted result posts input_candidate once with mapped payload", async () => {
  const { fetchImpl, calls, lastBody } = okFetch(okEvent());

  const admission = await postInputCandidate(
    input({ prompt: "do it", model: "m" }),
    options({ fetchImpl }),
  );

  assert.equal(calls(), 1);
  assert.equal(admission.tracked, true);
  assert.equal(admission.outcome, "admitted");
  assert.equal(admission.taskId, "task-1");
  assert.equal(admission.inputId, "msg_456");
  assert.equal(admission.eventId, EVENT_ID);
  const sent = lastBody() as Record<string, unknown>;
  assert.equal(sent["event_type"], "input_candidate");
  assert.equal(sent["event_id"], EVENT_ID);
  assert.equal(sent["adapter"], ADAPTER);
  assert.equal(sent["adapter_version"], ADAPTER_VERSION);
  assert.deepEqual(sent["payload"], {
    delivery: "new",
    prompt: "do it",
    model: "m",
  });
});

test("network failure fails open with CORE_UNAVAILABLE", async () => {
  const admission = await postInputCandidate(
    input(),
    options({
      fetchImpl: async () => {
        throw new Error("boom");
      },
    }),
  );

  assert.equal(admission.tracked, false);
  assert.equal(admission.outcome, "tracking_skipped");
  assert.equal(
    (admission as { diagnostic: string }).diagnostic,
    "CORE_UNAVAILABLE",
  );
  assert.equal(admission.eventId, EVENT_ID);
});

test("timeout abort fails open with CORE_UNAVAILABLE", async () => {
  const fetchImpl: FetchImpl = async (_url: string, init?: RequestInit) =>
    new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => {
        reject(new DOMException("aborted", "AbortError"));
      });
    });

  const admission = await postInputCandidate(
    input(),
    options({ fetchImpl, timeoutMs: 10 }),
  );

  assert.equal(admission.tracked, false);
  assert.equal(
    (admission as { diagnostic: string }).diagnostic,
    "CORE_UNAVAILABLE",
  );
});

test("non-2xx without valid envelope fails open", async () => {
  const admission = await postInputCandidate(
    input(),
    options({
      fetchImpl: async () => new Response("<html>oops</html>", { status: 500 }),
    }),
  );

  assert.equal(admission.tracked, false);
  assert.equal(
    (admission as { diagnostic: string }).diagnostic,
    "CORE_UNAVAILABLE",
  );
});

test("invalid 2xx envelope fails open", async () => {
  const admission = await postInputCandidate(
    input(),
    options({
      fetchImpl: async () =>
        new Response(JSON.stringify({ status: "ok", data: {} }), {
          status: 200,
        }),
    }),
  );

  assert.equal(admission.tracked, false);
  assert.equal(
    (admission as { diagnostic: string }).diagnostic,
    "CORE_UNAVAILABLE",
  );
});

test("unauthorized overlap stays untracked and preserves outcome", async () => {
  const { fetchImpl } = okFetch(
    okEvent({
      outcome: "released_overlap",
      input_id: null,
      task_id: null,
      dispatch_authorized: false,
    }),
  );

  const admission = await postInputCandidate(input(), options({ fetchImpl }));

  assert.equal(admission.tracked, false);
  assert.equal(admission.outcome, "released_overlap");
  assert.equal(
    (admission as { diagnostic: string }).diagnostic,
    "released_overlap",
  );
});

test("unauthorized steer error preserves code", async () => {
  let fetchCalls = 0;
  const fetchImpl: FetchImpl = async () => {
    fetchCalls += 1;
    return new Response(
      JSON.stringify({
        status: "error",
        message: "No active task.",
        data: { code: "STEER_WITHOUT_ACTIVE_TASK" },
      }),
      { status: 409 },
    );
  };

  const admission = await postInputCandidate(
    input({ delivery: "steer" }),
    options({ fetchImpl }),
  );

  assert.equal(fetchCalls, 1);
  assert.equal(admission.tracked, false);
  assert.equal(admission.outcome, "STEER_WITHOUT_ACTIVE_TASK");
  assert.equal(
    (admission as { diagnostic: string }).diagnostic,
    "STEER_WITHOUT_ACTIVE_TASK",
  );
});

test("mismatched success event_id fails open", async () => {
  const { fetchImpl } = okFetch(
    okEvent({ event_id: "123e4567-e89b-42d3-a456-426614174002" }),
  );

  const admission = await postInputCandidate(
    input(),
    options({ fetchImpl }),
  );

  assert.equal(admission.tracked, false);
  assert.equal(admission.outcome, "tracking_skipped");
  assert.equal(
    (admission as { diagnostic: string }).diagnostic,
    "CORE_UNAVAILABLE",
  );
  assert.equal(admission.eventId, EVENT_ID);
});

test("missing success event_id fails open", async () => {
  const body = okEvent();
  delete (
    body.data.event as Record<string, unknown>
  )["event_id"];
  const { fetchImpl } = okFetch(body);

  const admission = await postInputCandidate(
    input(),
    options({ fetchImpl }),
  );

  assert.equal(admission.tracked, false);
  assert.equal(admission.outcome, "tracking_skipped");
  assert.equal(
    (admission as { diagnostic: string }).diagnostic,
    "CORE_UNAVAILABLE",
  );
  assert.equal(admission.eventId, EVENT_ID);
});

test("admitted result without a task fails open", async () => {
  const { fetchImpl } = okFetch(okEvent({ task_id: null }));

  const admission = await postInputCandidate(input(), options({ fetchImpl }));

  assert.equal(admission.tracked, false);
  assert.equal(admission.outcome, "tracking_skipped");
  assert.equal(
    (admission as { diagnostic: string }).diagnostic,
    "CORE_UNAVAILABLE",
  );
});

test("admitted result without an input fails open", async () => {
  const { fetchImpl } = okFetch(okEvent({ input_id: null }));

  const admission = await postInputCandidate(input(), options({ fetchImpl }));

  assert.equal(admission.tracked, false);
  assert.equal(admission.outcome, "tracking_skipped");
  assert.equal(
    (admission as { diagnostic: string }).diagnostic,
    "CORE_UNAVAILABLE",
  );
});
