import assert from "node:assert/strict";
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { createServer } from "node:net";
import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { dispatchOpenCodeV1 } from "../src/opencode-v1/index.js";

const LIVE = process.env["CRUCIBLE_OPENCODE_LIVE"] === "1";
const SUPPORTED = "1.18.28";
const ADAPTER = "opencode-v1";
const ADAPTER_VERSION = "0.1.0";
const STARTUP_TIMEOUT_MS = 20000;
const STEP_TIMEOUT_MS = 15000;
const STOP_TIMEOUT_MS = 5000;
const TASKKILL_TIMEOUT_MS = 5000;

function freePort(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => {
        if (address && typeof address === "object") {
          resolvePort(address.port);
        } else {
          reject(new Error("NO_FREE_PORT"));
        }
      });
    });
  });
}

async function waitForHealth(url: string, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;

  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, {
        signal: AbortSignal.timeout(2000),
      });
      if (response.ok) {
        return;
      }
    } catch {
      await delay(250);
      continue;
    }
    await delay(250);
  }

  throw new Error(`STARTUP_TIMEOUT: ${url}`);
}

function isChildAlive(child: ChildProcess): boolean {
  return child.exitCode === null && child.signalCode === null;
}

function waitForChildExit(child: ChildProcess, timeoutMs: number): Promise<boolean> {
  if (!isChildAlive(child)) {
    return Promise.resolve(true);
  }

  return new Promise((resolve) => {
    let settled = false;
    const done = (exited: boolean) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      resolve(exited);
    };
    const timer = setTimeout(() => done(false), timeoutMs);
    // An 'error' listener here also prevents an uncaught 'error' event
    // if the child fails to spawn while a waiter is attached.
    child.once("error", () => done(true));
    child.once("exit", () => done(true));
    child.once("close", () => done(true));
  });
}

function watchSpawn(child: ChildProcess, byChild: WeakMap<ChildProcess, Error>): void {
  child.once("error", (error) => {
    byChild.set(child, error as Error);
  });
}

// Bounded cleanup scoped strictly to the owned spawned PID tree.
// Windows: taskkill targets only /PID <owned pid> /T (tree rooted at the
// owned child, which covers chocolatey shim descendants). No broad
// selectors (/IM, pkill, bare taskkill) are used.
async function stopChild(
  child: ChildProcess | undefined,
  label: string,
  failures: string[],
  spawnErrorByChild: WeakMap<ChildProcess, Error>,
): Promise<void> {
  if (!child) {
    return;
  }

  const spawnError = spawnErrorByChild.get(child);
  if (spawnError ?? child.pid === undefined) {
    failures.push(`${label} SPAWN_ERROR: ${spawnError?.message ?? "spawn failed"}`);
    return;
  }
  if (!isChildAlive(child)) {
    return;
  }

  const pid = child.pid;
  if (process.platform === "win32") {
    try {
      execFileSync("taskkill", ["/PID", String(pid), "/T", "/F"], {
        timeout: TASKKILL_TIMEOUT_MS,
        stdio: "ignore",
      });
    } catch (error) {
      if (isChildAlive(child)) {
        failures.push(
          `${label} TASKKILL_FAILED: ${(error as Error)?.message ?? String(error)}`,
        );
      }
    }

    const exited = await waitForChildExit(child, STOP_TIMEOUT_MS);
    if (!exited) {
      failures.push(`${label} CLEANUP_TIMEOUT: pid ${pid} tree did not exit`);
    }
    return;
  }

  try {
    child.kill("SIGTERM");
  } catch (error) {
    if (isChildAlive(child)) {
      failures.push(`${label} SIGTERM_FAILED: ${(error as Error)?.message ?? String(error)}`);
    }
    return;
  }

  const exited = await waitForChildExit(child, STOP_TIMEOUT_MS);
  if (!exited) {
    try {
      child.kill("SIGKILL");
    } catch (error) {
      failures.push(`${label} SIGKILL_FAILED: ${(error as Error)?.message ?? String(error)}`);
      return;
    }

    const killed = await waitForChildExit(child, STOP_TIMEOUT_MS);
    if (!killed) {
      failures.push(`${label} CLEANUP_TIMEOUT: pid ${pid} did not exit`);
    }
  }
}

test("live 1.18.28 admission/steer/overlap transport probe", { skip: !LIVE }, async () => {
  const binary = process.env["CRUCIBLE_OPENCODE_BIN"] ?? "opencode";
  let version = "";

  try {
    version = execFileSync(binary, ["--version"], {
      encoding: "utf8",
      timeout: 10000,
    }).trim();
  } catch {
    assert.fail(`OPENCODE_BINARY_UNAVAILABLE: ${binary}`);
  }
  assert.equal(version, SUPPORTED);

  const root = dirname(fileURLToPath(import.meta.url));
  const repoRoot = resolve(root, "..", "..", "..");
  const repo = mkdtempSync(join(tmpdir(), "crucible-live-repo-"));
  const coreData = mkdtempSync(join(tmpdir(), "crucible-live-core-"));
  const ocConf = mkdtempSync(join(tmpdir(), "crucible-live-conf-"));
  const ocData = mkdtempSync(join(tmpdir(), "crucible-live-ocdata-"));
  const ocState = mkdtempSync(join(tmpdir(), "crucible-live-ocstate-"));
  const ocCache = mkdtempSync(join(tmpdir(), "crucible-live-occache-"));

  let core: ChildProcess | undefined;
  let oc: ChildProcess | undefined;
  let bodyError: unknown;
  const spawnErrorByChild = new WeakMap<ChildProcess, Error>();

  try {
    execFileSync("git", ["init", "--quiet", repo], { timeout: 10000 });
    execFileSync("git", ["-C", repo, "config", "user.email", "probe@example.com"], {
      timeout: 10000,
    });
    execFileSync("git", ["-C", repo, "config", "user.name", "Probe"], {
      timeout: 10000,
    });

    const projectId = randomUUID();
    mkdirSync(join(repo, ".crucible"), { recursive: true });
    writeFileSync(
      join(repo, ".crucible", "project.json"),
      JSON.stringify({ project_id: projectId }),
    );
    execFileSync("git", ["-C", repo, "add", "."], { timeout: 10000 });
    execFileSync("git", ["-C", repo, "commit", "--quiet", "-m", "probe"], {
      timeout: 10000,
    });

    const corePort = await freePort();
    const ocPort = await freePort();
    const coreUrl = `http://127.0.0.1:${corePort}`;
    const ocUrl = `http://127.0.0.1:${ocPort}`;

    core = spawn(
      "python",
      ["-m", "uvicorn", "crucible_core.main:app", "--host", "127.0.0.1", "--port", String(corePort)],
      {
        cwd: join(repoRoot, "core"),
        env: {
          ...process.env,
          CRUCIBLE_DATA_DIR: coreData,
          PYTHONPATH: join(repoRoot, "core", "src"),
        },
        stdio: "ignore",
      },
    );
    watchSpawn(core, spawnErrorByChild);

    oc = spawn(binary, ["serve", "--port", String(ocPort), "--hostname", "127.0.0.1"], {
      env: {
        ...process.env,
        OPENCODE_DISABLE_AUTOUPDATE: "1",
        XDG_CONFIG_HOME: ocConf,
        XDG_DATA_HOME: ocData,
        XDG_STATE_HOME: ocState,
        XDG_CACHE_HOME: ocCache,
      },
      stdio: "ignore",
    });
    watchSpawn(oc, spawnErrorByChild);

    await waitForHealth(`${coreUrl}/v1/health`, STARTUP_TIMEOUT_MS);
    await waitForHealth(`${ocUrl}/global/health`, STARTUP_TIMEOUT_MS);

    // One isolated temporary Core + OpenCode pair serves the three
    // scenarios below in order, so the steer and overlap dispatches
    // observe the still-running Task created by the admission dispatch.
    // No terminal/completion event is sent, so the Task stays running.
    // Native OpenCode message IDs are only observed; they are never
    // used as Core event_id (the adapter mints a UUID per dispatch).
    const eventIds: string[] = [];
    const nativeIds: string[] = [];

    // Transport only: noReply persists the user message without
    // invoking a model provider. It proves HTTP transport and
    // native identity observation, not tool execution or terminal
    // ordering.
    async function createSession(): Promise<string> {
      const response = await fetch(`${ocUrl}/session`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-opencode-directory": repo,
        },
        body: JSON.stringify({ title: "live probe" }),
        signal: AbortSignal.timeout(STEP_TIMEOUT_MS),
      });
      assert.equal(response.ok, true);

      const body = (await response.json()) as { id: string };
      assert.match(body.id, /^ses_/);
      return body.id;
    }

    async function sendNoReply(
      sessionId: string,
      messageId: string,
      text: string,
    ): Promise<void> {
      const messageResponse = await fetch(
        `${ocUrl}/session/${sessionId}/message`,
        {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "x-opencode-directory": repo,
          },
          body: JSON.stringify({
            noReply: true,
            messageID: messageId,
            parts: [{ type: "text", text }],
          }),
          signal: AbortSignal.timeout(STEP_TIMEOUT_MS),
        },
      );
      assert.equal(messageResponse.ok, true);

      const messageBody = (await messageResponse.json()) as {
        info: { id: string };
      };
      const nativeId = messageBody.info.id;
      assert.equal(nativeId, messageId);
      for (const eventId of eventIds) {
        assert.notEqual(nativeId, eventId);
      }
      nativeIds.push(nativeId);
    }

    async function lookupEvent(eventId: string): Promise<{
      status: string;
      outcome: string;
      event_id: string;
      dispatch_authorized: boolean;
    }> {
      const lookup = await fetch(`${coreUrl}/v1/events/${eventId}`, {
        signal: AbortSignal.timeout(STEP_TIMEOUT_MS),
      });
      assert.equal(lookup.ok, true);

      const lookupBody = (await lookup.json()) as {
        data: { event: Record<string, unknown> };
      };
      const event = lookupBody.data.event;
      assert.equal(event["event_id"], eventId);
      return event as unknown as {
        status: string;
        outcome: string;
        event_id: string;
        dispatch_authorized: boolean;
      };
    }

    async function taskCount(): Promise<number> {
      const tasksResponse = await fetch(`${coreUrl}/v1/tasks?limit=50`, {
        signal: AbortSignal.timeout(STEP_TIMEOUT_MS),
      });
      assert.equal(tasksResponse.ok, true);

      const tasksBody = (await tasksResponse.json()) as {
        data: { tasks: Array<{ id: string }> };
      };
      return tasksBody.data.tasks.length;
    }

    // Scenario A: candidate/admission/Task ordering before dispatch.
    const eventA = randomUUID();
    eventIds.push(eventA);
    const inputA = `msg_live_${randomUUID().slice(0, 8)}`;
    const agentSession = await createSession();
    const order: string[] = [];

    const admitted = await dispatchOpenCodeV1(
      {
        openCodeVersion: SUPPORTED,
        agentSessionId: agentSession,
        messageId: inputA,
        workspacePath: repo,
        delivery: "new",
        prompt: "live transport probe",
      },
      {
        dispatch: async (context) => {
          assert.equal(context.tracked, true);
          assert.equal(context.eventId, eventA);

          const event = await lookupEvent(eventA);
          assert.equal(event.outcome, "admitted");
          assert.equal(event.dispatch_authorized, true);
          order.push("admission-durable");

          const tasksResponse = await fetch(`${coreUrl}/v1/tasks?limit=50`, {
            signal: AbortSignal.timeout(STEP_TIMEOUT_MS),
          });
          assert.equal(tasksResponse.ok, true);

          const tasksBody = (await tasksResponse.json()) as {
            data: { tasks: Array<{ id: string }> };
          };
          assert.ok(
            tasksBody.data.tasks.some((task) => task.id === context.taskId),
          );
          order.push("dispatch-start");

          await sendNoReply(agentSession, inputA, "live transport probe");
          order.push("dispatch-done");

          return "live-ok";
        },
      },
      {
        coreUrl,
        timeoutMs: STEP_TIMEOUT_MS,
        testing: { eventId: eventA },
      },
    );

    assert.equal(admitted.tracked, true);
    assert.equal(admitted.eventId, eventA);
    assert.deepEqual(order, ["admission-durable", "dispatch-start", "dispatch-done"]);
    const taskId = admitted.taskId;
    assert.ok(typeof taskId === "string" && taskId.length > 0);

    // Scenario B: steer attaches to the same active Task, then an
    // independent noReply request proves transport for the steered
    // Input against the same durable Task data.
    const eventS = randomUUID();
    eventIds.push(eventS);
    const inputS = `msg_live_${randomUUID().slice(0, 8)}`;

    const steered = await dispatchOpenCodeV1(
      {
        openCodeVersion: SUPPORTED,
        agentSessionId: agentSession,
        messageId: inputS,
        workspacePath: repo,
        delivery: "steer",
        prompt: "live steer probe",
      },
      {
        dispatch: async (context) => {
          assert.equal(context.tracked, true);
          assert.equal(context.eventId, eventS);
          assert.equal(context.taskId, taskId);

          const event = await lookupEvent(eventS);
          assert.equal(event.outcome, "admitted");
          assert.equal(event.dispatch_authorized, true);

          const detailResponse = await fetch(`${coreUrl}/v1/tasks/${taskId}`, {
            signal: AbortSignal.timeout(STEP_TIMEOUT_MS),
          });
          assert.equal(detailResponse.ok, true);

          const detailBody = (await detailResponse.json()) as {
            data: { task?: { input_ids: string[]; status: string } } & {
              input_ids?: string[];
              status?: string;
            };
          };
          const detail = detailBody.data.task ?? detailBody.data;
          assert.equal(detail.status, "running");
          assert.ok(detail.input_ids !== undefined);
          const inputIds: string[] = detail.input_ids;
          assert.ok(inputIds.includes(inputA));
          assert.ok(inputIds.includes(inputS));

          await sendNoReply(agentSession, inputS, "live steer probe");

          return "live-steer-ok";
        },
      },
      {
        coreUrl,
        timeoutMs: STEP_TIMEOUT_MS,
        testing: { eventId: eventS },
      },
    );

    assert.equal(steered.tracked, true);
    assert.equal(steered.taskId, taskId);
    assert.equal(await taskCount(), 1);

    // Scenario C: a different session with delivery=new while the Task
    // is running is excluded as released_overlap (no trustworthy Task),
    // but the real noReply request is still sent, untracked.
    const eventO = randomUUID();
    eventIds.push(eventO);
    const inputO = `msg_live_${randomUUID().slice(0, 8)}`;
    const overlapSession = await createSession();

    const overlapped = await dispatchOpenCodeV1(
      {
        openCodeVersion: SUPPORTED,
        agentSessionId: overlapSession,
        messageId: inputO,
        workspacePath: repo,
        delivery: "new",
        prompt: "live overlap probe",
      },
      {
        dispatch: async (context) => {
          assert.equal(context.tracked, false);
          assert.equal(context.eventId, eventO);
          assert.equal(context.taskId, null);

          const event = await lookupEvent(eventO);
          assert.equal(event.outcome, "released_overlap");
          assert.equal(event.dispatch_authorized, false);

          await sendNoReply(overlapSession, inputO, "live overlap probe");

          return "live-overlap-ok";
        },
      },
      {
        coreUrl,
        timeoutMs: STEP_TIMEOUT_MS,
        testing: { eventId: eventO },
      },
    );

    assert.equal(overlapped.tracked, false);
    assert.equal(
      (overlapped as { diagnostic?: string }).diagnostic,
      "released_overlap",
    );
    assert.equal(overlapped.taskId, null);
    assert.equal(await taskCount(), 1);
    assert.equal(new Set(nativeIds).size, 3);

    console.log(
      JSON.stringify({
        probe: "opencode-v1-live",
        opencodeVersion: version,
        adapter: ADAPTER,
        adapterVersion: ADAPTER_VERSION,
        taskId,
        scenarios: ["admission-before-dispatch", "steer-joins-task", "overlap-excluded"],
        eventIds,
        nativeIds,
      }),
    );
  } catch (error) {
    bodyError = error;
    throw error;
  } finally {
    const cleanupFailures: string[] = [];
    await stopChild(oc, "opencode", cleanupFailures, spawnErrorByChild);
    await stopChild(core, "core", cleanupFailures, spawnErrorByChild);

    for (const directory of [
      repo,
      coreData,
      ocConf,
      ocData,
      ocState,
      ocCache,
    ]) {
      try {
        rmSync(directory, { recursive: true, force: true });
      } catch (error) {
        cleanupFailures.push(
          `RM_FAILED ${directory}: ${(error as Error)?.message ?? String(error)}`,
        );
      }
    }

    if (cleanupFailures.length > 0) {
      const message = `CLEANUP_FAILED: ${cleanupFailures.join("; ")}`;
      if (bodyError === undefined) {
        assert.fail(message);
      } else {
        console.error(message);
      }
    }
  }
});
