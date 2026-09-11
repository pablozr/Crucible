import assert from "node:assert/strict";
import type { SpawnOptions } from "node:child_process";
import { mkdtempSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { resolveDashboardDir } from "../src/commands/dashboard.js";
import { runServe } from "../src/commands/serve.js";
import { getCliProductVersion } from "../src/runtime/version.js";
import { STATUS_URL, runStatus } from "../src/commands/status.js";
import { isMainModule, runCli, USAGE } from "../src/index.js";
import type { ForegroundExit } from "../src/shared/foreground-process.js";


type SpawnCall = { command: string; args: string[]; options: SpawnOptions };

function okRunner(calls: SpawnCall[]): (command: string, args: string[], options: SpawnOptions) => Promise<ForegroundExit> {
  return async (command, args, options) => {
    calls.push({ command, args, options });
    return { code: 0, signal: null };
  };
}

function successBody() {
  return {
    status: "ok",
    message: "Service status retrieved.",
    data: {
      system: {
        status: "operational",
        version: "0.1.0",
        api_version: "v1",
        address: "http://127.0.0.1:7331",
        database: {
          path: "/data/crucible.db",
          migration_revision: "0008",
          journal_mode: "wal",
          synchronous: "full",
          foreign_keys: true,
        },
      },
    },
  };
}

function jsonResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: async () => body,
  } as Response;
}


test("serve spawns the resolved executable without args and inherits stdio", async () => {
  const calls: SpawnCall[] = [];
  const executable = process.platform === "win32"
    ? "C:\\runtime\\crucible-core.exe"
    : "/opt/runtime/crucible-core";
  await runServe({ runner: okRunner(calls), resolveExecutable: () => executable });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].command, executable);
  assert.deepEqual(calls[0].args, []);
  assert.equal(calls[0].options.stdio, "inherit");
  assert.equal(calls[0].options.shell, false);
  assert.equal(calls[0].options.detached, false);
  assert.ok(!("cwd" in calls[0].options) || calls[0].options.cwd === undefined);
});

test("serve resolves a dev override from the environment", async () => {
  const dir = mkdtempSync(join(tmpdir(), "crucible-serve-env-"));
  const override = join(dir, process.platform === "win32" ? "core.exe" : "core");
  writeFileSync(override, "fake\n");

  const calls: SpawnCall[] = [];
  await runServe({
    runner: okRunner(calls),
    env: { CRUCIBLE_CORE_EXECUTABLE: override },
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].command, override);
});

test("serve surfaces resolution failures without spawning", async () => {
  const calls: SpawnCall[] = [];
  await assert.rejects(
    () => runServe({ runner: okRunner(calls), env: { CRUCIBLE_CORE_EXECUTABLE: "relative/path" } }),
    /CRUCIBLE_CORE_EXECUTABLE.*absolute/,
  );
  assert.equal(calls.length, 0);
});

test("serve reports spawn failure for the resolved executable", async () => {
  const missing = async () => {
    const error = new Error("spawn ENOENT") as NodeJS.ErrnoException;
    error.code = "ENOENT";
    throw error;
  };

  await assert.rejects(
    () => runServe({ runner: missing, resolveExecutable: () => "/opt/runtime/crucible-core" }),
    /executable not found/,
  );
});

test("serve reports nonzero exit", async () => {
  const failing = async (): Promise<ForegroundExit> => ({ code: 3, signal: null });
  await assert.rejects(
    () => runServe({ runner: failing, resolveExecutable: () => "/opt/runtime/crucible-core" }),
    /exited with code 3/,
  );
});

test("--version prints the package version with no extras", async () => {
  const lines: string[] = [];
  const code = await runCli(["--version"], {
    stdout: (line) => lines.push(line),
    stderr: () => {},
  });

  assert.equal(code, 0);
  assert.deepEqual(lines, [getCliProductVersion()]);
  assert.equal(getCliProductVersion(), "0.1.0");
});

test("--version rejects extras", async () => {
  const errors: string[] = [];
  assert.equal(await runCli(["--version", "extra"], { stderr: (m) => errors.push(m) }), 1);
  assert.ok(errors[0].includes(USAGE.split("\n")[0]));
});

test("dashboard assets resolve adjacent to the CLI package", async () => {
  const cliRoot = resolve(join("repo", "packages", "cli"));
  const fromSrc = resolveDashboardDir(pathToFileURL(join(cliRoot, "src", "commands", "dashboard.ts")).href);
  const fromDist = resolveDashboardDir(pathToFileURL(join(cliRoot, "dist", "commands", "dashboard.js")).href);

  assert.equal(fromSrc, fromDist);
});

test("status prints deterministic text", async () => {
  const body = successBody();
  const lines: string[] = [];
  let calls = 0;
  let seenUrl: string | undefined;

  const fetchFn = (async (url: string | URL | Request, init?: RequestInit) => {
    calls += 1;
    seenUrl = String(url);
    assert.ok(init?.signal);
    return jsonResponse(body);
  }) as typeof fetch;

  await runStatus({ json: false }, { fetchFn, stdout: (line) => lines.push(line) });

  assert.equal(calls, 1);
  assert.equal(seenUrl, STATUS_URL);
  assert.equal(lines.length, 1);
  const text = lines[0];
  for (const expected of [
    "operational",
    "0.1.0",
    "v1",
    "http://127.0.0.1:7331",
    "/data/crucible.db",
    "0008",
    "wal",
    "full",
    "true",
  ]) {
    assert.ok(text.includes(expected), `missing ${expected}`);
  }
});

test("status --json prints the envelope", async () => {
  const body = successBody();
  const lines: string[] = [];

  await runStatus(
    { json: true },
    { fetchFn: (async () => jsonResponse(body)) as typeof fetch, stdout: (line) => lines.push(line) },
  );

  assert.equal(lines.length, 1);
  assert.equal(lines[0], JSON.stringify(body, null, 2));
});

test("status surfaces API error message and code only", async () => {
  const lines: string[] = [];
  const fetchFn = (async () =>
    jsonResponse({ status: "error", message: "API route not found.", data: { code: "API_ROUTE_NOT_FOUND" } }, false, 404)) as typeof fetch;

  await assert.rejects(
    () => runStatus({ json: false }, { fetchFn, stdout: (line) => lines.push(line) }),
    (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.match(error.message, /API route not found\./);
      assert.match(error.message, /API_ROUTE_NOT_FOUND/);
      return true;
    },
  );
  assert.equal(lines.length, 0);
});

test("status maps network failure to crucible serve", async () => {
  const fetchFn = (async () => {
    throw new TypeError("fetch failed");
  }) as typeof fetch;

  await assert.rejects(() => runStatus({ json: false }, { fetchFn, stdout: () => {} }), /crucible serve/);
});

test("status rejects malformed envelope", async () => {
  const fetchFn = (async () =>
    jsonResponse({ status: "ok", message: "x", data: { system: { status: "operational" } } })) as typeof fetch;

  await assert.rejects(
    () => runStatus({ json: false }, { fetchFn, stdout: () => {} }),
    /Invalid status response/,
  );
});

test("parser rejects unknown commands and extras", async () => {
  const errors: string[] = [];
  const deps = { stderr: (message: string) => errors.push(message) };

  assert.equal(await runCli(["nope"], deps), 1);
  assert.equal(await runCli([], deps), 1);
  assert.equal(await runCli(["serve", "extra"], deps), 1);
  assert.equal(await runCli(["dashboard", "extra"], deps), 1);
  assert.equal(await runCli(["init", "a", "b"], deps), 1);
  assert.equal(await runCli(["status", "--json", "extra"], deps), 1);
  assert.equal(await runCli(["status", "--other"], deps), 1);
  for (const message of errors) {
    assert.ok(message.includes(USAGE.split("\n")[0]));
  }
  assert.ok(USAGE.includes("init [directory]"));
  assert.ok(USAGE.includes("serve"));
  assert.ok(USAGE.includes("dashboard"));
  assert.ok(USAGE.includes("status [--json]"));
});

test("parser dispatches status --json with injected fetch", async () => {
  const body = successBody();
  const lines: string[] = [];

  const code = await runCli(["status", "--json"], {
    fetchFn: (async () => jsonResponse(body)) as typeof fetch,
    stdout: (line) => lines.push(line),
    stderr: () => {},
  });

  assert.equal(code, 0);
  assert.equal(lines[0], JSON.stringify(body, null, 2));
});

test("parser reports command failures with exit 1", async () => {
  const errors: string[] = [];
  const failingFetch = (async () => {
    throw new TypeError("fetch failed");
  }) as typeof fetch;

  assert.equal(await runCli(["status"], { fetchFn: failingFetch, stdout: () => {}, stderr: (m) => errors.push(m) }), 1);
  assert.match(errors[0], /crucible serve/);
});

test("status hides arbitrary non-2xx body and reports generic failure", async () => {
  const lines: string[] = [];
  const payload = "<script>alert('pwned')</script>";
  const fetchFn = (async () =>
    jsonResponse({ message: payload, data: { code: "EVIL", extra: payload } }, false, 500)) as typeof fetch;

  await assert.rejects(
    () => runStatus({ json: false }, { fetchFn, stdout: (line) => lines.push(line) }),
    (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.equal(error.message, "Core error: request failed with status 500.");
      assert.ok(!error.message.includes(payload), "arbitrary body must not leak");
      return true;
    },
  );
  assert.equal(lines.length, 0);
});

test("isMainModule resolves direct paths", () => {
  const dir = mkdtempSync(join(tmpdir(), "crucible-main-"));
  const real = join(dir, "index.js");
  writeFileSync(real, "export {};\n");

  assert.equal(isMainModule(real, pathToFileURL(real).href), true);
  assert.equal(isMainModule(join(dir, "other.js"), pathToFileURL(real).href), false);
  assert.equal(isMainModule(undefined, pathToFileURL(real).href), false);
});

test("isMainModule follows symlinks to the entrypoint", async (t) => {
  const dir = mkdtempSync(join(tmpdir(), "crucible-main-link-"));
  const real = join(dir, "index.js");
  writeFileSync(real, "export {};\n");
  const link = join(dir, "linked-index.js");

  try {
    symlinkSync(real, link);
  } catch {
    t.skip("symlinks not permitted on this platform");
    return;
  }

  assert.equal(isMainModule(link, pathToFileURL(real).href), true);
});

test("isMainModule follows directory junctions to the entrypoint", async (t) => {
  const dir = mkdtempSync(join(tmpdir(), "crucible-main-junction-"));
  const realDir = join(dir, "real");
  const { mkdirSync } = await import("node:fs");
  mkdirSync(realDir, { recursive: true });
  const real = join(realDir, "index.js");
  writeFileSync(real, "export {};\n");
  const junction = join(dir, "junction");

  try {
    symlinkSync(realDir, junction, "junction");
  } catch {
    t.skip("junctions not permitted on this platform");
    return;
  }

  assert.equal(isMainModule(join(junction, "index.js"), pathToFileURL(real).href), true);
});
