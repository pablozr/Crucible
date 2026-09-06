import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { dispatchOpenCodeV1 } from "../src/dispatch.js";
import { resolveProject } from "../src/project.js";

const PROJECT_ID = "123e4567-e89b-42d3-a456-426614174000";

function request(overrides = {}) {
  return {
    openCodeVersion: "1.18.28",
    agentSessionId: "ses_123",
    messageId: "msg_456",
    workspacePath: "/repo",
    delivery: "new" as const,
    ...overrides,
  };
}

function stubResolve() {
  return async () => ({ projectId: PROJECT_ID, gitRoot: "/repo" });
}

function okEvent(overrides = {}) {
  return {
    status: "ok",
    message: "Event received.",
    data: {
      event: {
        event_id: "evt-1",
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
  const fetchImpl = async (_url: string, init?: RequestInit) => {
    calls += 1;
    lastBody = JSON.parse(String(init?.body));
    return new Response(JSON.stringify(body), { status: 200 });
  };
  return { fetchImpl, calls: () => calls, lastBody: () => lastBody };
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

test("dispatch waits for admitted Core result (no dispatch before Core resolves)", async () => {
  const order: string[] = [];
  let resolveFetch!: (r: Response) => void;
  const gate = new Promise<Response>((resolve) => {
    resolveFetch = resolve;
  });
  const fetchImpl = async () => {
    order.push("fetch-start");
    const response = await gate;
    order.push("fetch-resolve");
    return response;
  };
  const { fn, calls } = dispatchStub((() => {
    order.push("dispatch");
    return "sent";
  }) as (ctx: unknown) => string);

  const pending = dispatchOpenCodeV1(request(), {
    dispatch: fn,
    fetchImpl,
    resolveProject: stubResolve(),
  });
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(calls.length, 0);

  resolveFetch(new Response(JSON.stringify(okEvent()), { status: 200 }));
  const result = await pending;

  assert.equal(result.tracked, true);
  assert.deepEqual(order, ["fetch-start", "fetch-resolve", "dispatch"]);
});

test("valid admitted Core result is tracked and posts input_candidate once", async () => {
  const { fetchImpl, calls, lastBody } = okFetch(okEvent());
  const { fn, calls: dispatchCalls } = dispatchStub();

  const result = await dispatchOpenCodeV1(
    request({ prompt: "do it", model: "m" }),
    { dispatch: fn, fetchImpl, resolveProject: stubResolve() },
    { eventId: "evt-1", coreUrl: "http://127.0.0.1:7331" },
  );

  assert.equal(calls(), 1);
  assert.equal(dispatchCalls.length, 1);
  assert.equal(result.tracked, true);
  assert.equal(result.outcome, "admitted");
  assert.equal(result.taskId, "task-1");
  const sent = lastBody() as Record<string, unknown>;
  assert.equal(sent["event_type"], "input_candidate");
  assert.equal(sent["event_id"], "evt-1");
  assert.deepEqual(sent["payload"], {
    delivery: "new",
    prompt: "do it",
    model: "m",
  });
});

test("core network failure fails open with CORE_UNAVAILABLE", async () => {
  const fetchImpl = async () => {
    throw new Error("boom");
  };
  const { fn, calls } = dispatchStub();

  const result = await dispatchOpenCodeV1(request(), {
    dispatch: fn,
    fetchImpl,
    resolveProject: stubResolve(),
  });

  assert.equal(result.tracked, false);
  assert.equal(result.diagnostic, "CORE_UNAVAILABLE");
  assert.equal(calls.length, 1);
});

test("core timeout fails open with CORE_UNAVAILABLE", async () => {
  const fetchImpl = async (_url: string, init?: RequestInit) =>
    new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => {
        reject(new DOMException("aborted", "AbortError"));
      });
    });
  const { fn, calls } = dispatchStub();

  const result = await dispatchOpenCodeV1(request(), {
    dispatch: fn,
    fetchImpl,
    resolveProject: stubResolve(),
  }, { timeoutMs: 10 });

  assert.equal(result.tracked, false);
  assert.equal(result.diagnostic, "CORE_UNAVAILABLE");
  assert.equal(calls.length, 1);
});

test("core non-2xx without valid envelope fails open", async () => {
  const fetchImpl = async () =>
    new Response("<html>oops</html>", { status: 500 });
  const { fn, calls } = dispatchStub();

  const result = await dispatchOpenCodeV1(request(), {
    dispatch: fn,
    fetchImpl,
    resolveProject: stubResolve(),
  });

  assert.equal(result.tracked, false);
  assert.equal(result.diagnostic, "CORE_UNAVAILABLE");
  assert.equal(calls.length, 1);
});

test("core invalid 2xx envelope fails open", async () => {
  const fetchImpl = async () =>
    new Response(JSON.stringify({ status: "ok", data: {} }), { status: 200 });
  const { fn } = dispatchStub();

  const result = await dispatchOpenCodeV1(request(), {
    dispatch: fn,
    fetchImpl,
    resolveProject: stubResolve(),
  });

  assert.equal(result.tracked, false);
  assert.equal(result.diagnostic, "CORE_UNAVAILABLE");
});

test("valid unauthorized overlap stays untracked and preserves outcome", async () => {
  const { fetchImpl } = okFetch(
    okEvent({
      outcome: "released_overlap",
      input_id: null,
      task_id: null,
      dispatch_authorized: false,
    }),
  );
  const { fn, calls } = dispatchStub();

  const result = await dispatchOpenCodeV1(request(), {
    dispatch: fn,
    fetchImpl,
    resolveProject: stubResolve(),
  });

  assert.equal(result.tracked, false);
  assert.equal(result.outcome, "released_overlap");
  assert.equal(result.diagnostic, "released_overlap");
  assert.equal(calls.length, 1);
});

test("valid unauthorized steer error stays untracked and preserves code", async () => {
  let fetchCalls = 0;
  const fetchImpl = async () => {
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
  const { fn, calls } = dispatchStub();

  const result = await dispatchOpenCodeV1(request({ delivery: "steer" }), {
    dispatch: fn,
    fetchImpl,
    resolveProject: stubResolve(),
  });

  assert.equal(fetchCalls, 1);
  assert.equal(result.tracked, false);
  assert.equal(result.diagnostic, "STEER_WITHOUT_ACTIVE_TASK");
  assert.equal(calls.length, 1);
});

test("wrong OpenCode version dispatches untracked without Core call", async () => {
  let fetchCalls = 0;
  let resolveCalls = 0;
  const fetchImpl = async () => {
    fetchCalls += 1;
    return new Response(JSON.stringify(okEvent()), { status: 200 });
  };
  const { fn, calls } = dispatchStub();

  const result = await dispatchOpenCodeV1(request({ openCodeVersion: "1.18.29" }), {
    dispatch: fn,
    fetchImpl,
    resolveProject: async () => {
      resolveCalls += 1;
      return { projectId: PROJECT_ID, gitRoot: "/repo" };
    },
  });

  assert.equal(result.tracked, false);
  assert.equal(result.diagnostic, "INCOMPATIBLE_OPENCODE");
  assert.equal(fetchCalls, 0);
  assert.equal(resolveCalls, 0);
  assert.equal(calls.length, 1);
});

test("dispatch exceptions are not swallowed", async () => {
  const { fetchImpl } = okFetch(okEvent());
  let fetchCalls = 0;
  const countingFetch = async (url: string, init?: RequestInit) => {
    fetchCalls += 1;
    return fetchImpl(url, init);
  };

  await assert.rejects(
    dispatchOpenCodeV1(request(), {
      dispatch: async () => {
        throw new Error("V1_DOWN");
      },
      fetchImpl: countingFetch,
      resolveProject: stubResolve(),
    }),
    /V1_DOWN/,
  );
  assert.equal(fetchCalls, 1);
});

function gitRepository(): string {
  const root = mkdtempSync(join(tmpdir(), "crucible-adapter-"));
  execFileSync("git", ["init", "--quiet", root]);
  return root;
}

test("project resolver is read-only and resolves UUID", async () => {
  const root = gitRepository();
  const directory = join(root, ".crucible");
  mkdirSync(directory);
  const path = join(directory, "project.json");
  writeFileSync(path, `${JSON.stringify({ project_id: PROJECT_ID }, null, 2)}\n`);
  const beforeContent = readFileSync(path, "utf8");
  const beforeStat = statSync(path);
  const beforeEntries = readdirSync(directory).sort();

  const resolved = resolveProject(root);
  assert.equal(resolved.projectId, PROJECT_ID);

  const { fetchImpl } = okFetch(okEvent());
  const { fn } = dispatchStub();
  const result = await dispatchOpenCodeV1(
    request({ workspacePath: root }),
    { dispatch: fn, fetchImpl, resolveProject },
  );
  assert.equal(result.tracked, true);

  assert.equal(readFileSync(path, "utf8"), beforeContent);
  assert.equal(statSync(path).mtimeMs, beforeStat.mtimeMs);
  assert.deepEqual(readdirSync(directory).sort(), beforeEntries);
});

test("uninitialized workspace fails open without writes or Core call", async () => {
  const root = gitRepository();
  let fetchCalls = 0;
  const fetchImpl = async () => {
    fetchCalls += 1;
    return new Response(JSON.stringify(okEvent()), { status: 200 });
  };
  const { fn, calls } = dispatchStub();

  const result = await dispatchOpenCodeV1(request({ workspacePath: root }), {
    dispatch: fn,
    fetchImpl,
    resolveProject,
  });

  assert.equal(result.tracked, false);
  assert.match(result.diagnostic ?? "", /UNINITIALIZED_PROJECT/);
  assert.equal(fetchCalls, 0);
  assert.equal(calls.length, 1);
  assert.equal(readdirSync(root).sort().includes(".crucible"), false);
});
