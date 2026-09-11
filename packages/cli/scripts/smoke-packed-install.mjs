// Smoke test for the packed CLI + runtime tarballs without publishing.
//
// Usage:
//   node scripts/smoke-packed-install.mjs [--cli-tarball <path> --runtime-tarball <path>]
//                                         [--keep-temp] [--timeout-ms <n>]
//
// When tarballs are omitted they are produced locally with `pnpm pack` for
// packages/cli (pnpm rewrites the workspace: optionalDependencies to exact
// versions; npm pack would keep the workspace: protocol verbatim) and
// `npm pack` for the host runtime package (win32-x64 on this machine, or
// --runtime-tarball elsewhere). The script then:
//   1. Creates an isolated temp npm prefix + cache.
//   2. Installs those tarballs plus workspace-vendored dependency tarballs
//      offline with --ignore-scripts (no network, no lifecycle scripts) so
//      the CLI must resolve the sibling optional runtime package via
//      createRequire, exactly like a real install.
//   3. Asserts packed dashboard assets exist and are served by the packed
//      static handler (no browser, no persistent server).
//   4. Asserts the packed dist never spawns python/pnpm/npx/ng/tsc.
//   5. Runs `crucible --version` via process.execPath (no shell, no .bin shims).
//   6. Runs `crucible serve` with a temp CRUCIBLE_DATA_DIR, polls
//      /v1/status until healthy, then shuts the child down safely.
//
// Temp dirs and child processes are always cleaned up (unless --keep-temp).
// All spawns use shell: false and process.execPath; npm runs via its bundled
// npm-cli.js (resolved across the bin/lib layouts shipped by installers and
// setup-node) and pnpm runs via Node's bundled corepack pnpm.js so no shell
// is needed on Windows either (spawning bare `npm`/`pnpm` with shell: false
// fails there: only npm.CMD/pnpm.CMD exist on PATH, and spawning .CMD without
// a shell throws EINVAL on modern Node).
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import {
  existsSync,
  mkdtempSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { gunzipSync } from "node:zlib";

const CLI_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const WORKSPACE_ROOT = resolve(CLI_ROOT, "..", "..");

const STATUS_URL = "http://127.0.0.1:7331/v1/status";
// Frozen-binary cold start includes migrations; stay generous but bounded.
const DEFAULT_TIMEOUT_MS = 90_000;
const POLL_INTERVAL_MS = 500;

function usage() {
  return [
    "Usage: node scripts/smoke-packed-install.mjs [options]",
    "",
    "Options:",
    "  --cli-tarball <path>      Local @pablozrrrr/cli tarball (default: pnpm pack packages/cli)",
    "  --runtime-tarball <path>  Local @pablozrrrr/core-<target> tarball (default: npm pack the host package)",
    "  --timeout-ms <n>          Max wait for core status (default 90000)",
    "  --keep-temp               Keep the temp dir for inspection on success",
  ].join("\n");
}

function parseArgs(argv) {
  const args = {
    cliTarball: undefined,
    runtimeTarball: undefined,
    timeoutMs: DEFAULT_TIMEOUT_MS,
    keepTemp: false,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    if (flag === "--cli-tarball") args.cliTarball = argv[++i];
    else if (flag === "--runtime-tarball") args.runtimeTarball = argv[++i];
    else if (flag === "--timeout-ms") args.timeoutMs = Number(argv[++i]);
    else if (flag === "--keep-temp") args.keepTemp = true;
    else if (flag === "--help" || flag === "-h") {
      console.log(usage());
      process.exit(0);
    } else throw new Error(`Unknown argument ${JSON.stringify(flag)}.\n${usage()}`);
  }
  if (args.cliTarball === undefined && args.runtimeTarball !== undefined) {
    throw new Error("Pass --cli-tarball together with --runtime-tarball, or omit both to pack locally.");
  }
  if (!Number.isFinite(args.timeoutMs) || args.timeoutMs <= 0) {
    throw new Error("--timeout-ms must be a positive number.");
  }
  return args;
}

function log(step) {
  console.log(`[packed-smoke] ${step}`);
}

/** npm inherits pnpm-set npm_config_* env vars; drop them so the fixture is hermetic. */
function cleanNpmEnv(env) {
  const out = { ...env };
  for (const key of Object.keys(out)) {
    if (key.startsWith("npm_config_")) delete out[key];
  }
  return out;
}

/** Run a command with shell: false, capturing output; reject on nonzero exit. */
function runCapture(command, args, options = {}) {
  return new Promise((promiseResolve, promiseReject) => {
    const child = spawn(command, args, { shell: false, windowsHide: true, ...options });
    let stdout = "";
    let stderr = "";
    child.stdout?.on("data", (chunk) => {
      stdout += String(chunk);
    });
    child.stderr?.on("data", (chunk) => {
      stderr += String(chunk);
    });
    child.on("error", promiseReject);
    child.on("close", (code, signal) => {
      if (code === 0) promiseResolve({ stdout, stderr });
      else {
        const detail = (stderr.trim() || stdout.trim() || `signal ${signal}`).slice(-3000);
        promiseReject(new Error(`${basename(command)} ${args.join(" ")} exited with code ${code}: ${detail}`));
      }
    });
  });
}

/**
 * npm's bundled CLI. setup-node (and most installers) place node at
 * <prefix>/bin/node with npm at <prefix>/lib/node_modules/npm, not next to
 * process.execPath, so probe each known layout instead of a single path.
 * Running npm-cli.js with process.execPath keeps shell: false working on
 * Windows (spawning npm/npm.cmd from PATH without a shell fails there with
 * ENOENT/EINVAL on modern Node).
 */
function npmCliJs() {
  const execDir = dirname(process.execPath);
  const candidates = [
    join(execDir, "node_modules", "npm", "bin", "npm-cli.js"),
    resolve(execDir, "..", "lib", "node_modules", "npm", "bin", "npm-cli.js"),
  ];
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error(
    `Cannot locate npm's npm-cli.js (tried ${candidates.join(", ")}); install Node.js with npm to run the packed smoke.`,
  );
}

async function npmPack(packageDir, outDir) {
  mkdirSync(outDir, { recursive: true });
  const { stdout } = await runCapture(
    process.execPath,
    [npmCliJs(), "pack", "--pack-destination", outDir, "--silent"],
    { cwd: packageDir, env: cleanNpmEnv(process.env) },
  );
  const file = stdout.trim().split(/\r?\n/).filter(Boolean).at(-1);
  if (!file) throw new Error(`npm pack produced no output in ${packageDir}.`);
  return resolve(outDir, file);
}

/**
 * Node's bundled corepack shim for pnpm. Spawning bare `pnpm` with
 * shell: false fails on Windows (ENOENT: only pnpm.CMD is on PATH, which
 * needs a shell), and `corepack pnpm` would need a shell too, so run the
 * shim with process.execPath instead. This works on Windows and CI without
 * depending on a global pnpm command name.
 */
function pnpmViaCorepackJs() {
  const bundled = join(dirname(process.execPath), "node_modules", "corepack", "dist", "pnpm.js");
  if (existsSync(bundled)) return bundled;
  throw new Error(
    "Cannot locate corepack's pnpm.js next to process.execPath; install Node.js with corepack to run the packed smoke.",
  );
}

/**
 * Pack @pablozrrrr/cli with pnpm (never npm): source optionalDependencies use
 * the workspace: protocol, which npm pack keeps verbatim in the tarball
 * while pnpm pack rewrites to the exact version. Dependency vendoring and
 * the runtime tarball stay on npm pack. pnpm pack prints a human-readable
 * report instead of a bare filename, so resolve the new tarball by diffing
 * the output dir.
 */
async function packCli(packageDir, outDir) {
  mkdirSync(outDir, { recursive: true });
  const before = new Set(readdirSync(outDir));
  await runCapture(process.execPath, [pnpmViaCorepackJs(), "pack", "--pack-destination", outDir], {
    cwd: packageDir,
    env: { ...cleanNpmEnv(process.env), COREPACK_ENABLE_DOWNLOAD_PROMPT: "0" },
  });
  const created = readdirSync(outDir).filter((entry) => entry.endsWith(".tgz") && !before.has(entry));
  if (created.length !== 1) {
    throw new Error(`pnpm pack in ${packageDir} produced ${created.length} new tarballs (want exactly 1).`);
  }
  return resolve(outDir, created[0]);
}

/** Read package/package.json straight out of a packed .tgz (stdlib only). */
function readTarballPackageJson(tarballPath) {
  let entries;
  try {
    entries = gunzipSync(readFileSync(tarballPath));
  } catch (error) {
    throw new Error(`Cannot gunzip tarball at ${tarballPath}: ${error instanceof Error ? error.message : String(error)}`);
  }
  let offset = 0;
  while (offset + 512 <= entries.length) {
    const header = entries.subarray(offset, offset + 512);
    if (header.every((byte) => byte === 0)) break;
    const name = header.subarray(0, 100).toString("utf8").replace(/\0.*$/s, "");
    const size = Number.parseInt(header.subarray(124, 136).toString("utf8").replace(/\0.*$/s, "").trim(), 8) || 0;
    const dataStart = offset + 512;
    if (name === "package/package.json") {
      return JSON.parse(entries.subarray(dataStart, dataStart + size).toString("utf8"));
    }
    offset = dataStart + Math.ceil(size / 512) * 512;
  }
  throw new Error(`package/package.json not found in ${tarballPath}.`);
}

/**
 * Fail closed when the CLI tarball still carries the workspace: protocol
 * (i.e. it was packed with npm instead of pnpm). Applies to locally packed
 * and --cli-tarball-provided tarballs alike.
 */
function assertCliTarballOptionalDepsExact(tarballPath) {
  const manifest = readTarballPackageJson(tarballPath);
  const optional = manifest.optionalDependencies ?? {};
  const expected = {
    "@pablozrrrr/core-win32-x64": "0.1.0",
    "@pablozrrrr/core-darwin-x64": "0.1.0",
    "@pablozrrrr/core-darwin-arm64": "0.1.0",
    "@pablozrrrr/core-linux-x64-gnu": "0.1.0",
  };
  for (const [name, version] of Object.entries(expected)) {
    if (optional[name] !== version) {
      throw new Error(
        `CLI tarball ${tarballPath} has optionalDependencies[${JSON.stringify(name)}] = ${JSON.stringify(optional[name])}, want ${JSON.stringify(version)} (pack @pablozrrrr/cli with pnpm so workspace: is rewritten).`,
      );
    }
  }
  log(`CLI tarball optionalDependencies are exact (${Object.values(expected)[0]} x4)`);
}

/**
 * Vendor the CLI's runtime dependencies as local tarballs packed from this
 * workspace (resolved exactly as Node would resolve them here). The fixture
 * install is offline from an empty cache, so registry deps must arrive as
 * files — never via the network. This does not mask a missing declaration:
 * the installed manifest assertion below still fails closed when
 * packages/cli/package.json omits a dependency.
 */
async function vendorWorkspaceDepTarballs(outDir) {
  const sourcePkg = JSON.parse(readFileSync(join(CLI_ROOT, "package.json"), "utf8"));
  const specs = sourcePkg.dependencies ?? {};
  const requireFromCli = createRequire(pathToFileURL(join(CLI_ROOT, "package.json")).href);
  const tarballs = [];
  for (const name of Object.keys(specs)) {
    let sourceDir;
    try {
      sourceDir = dirname(requireFromCli.resolve(`${name}/package.json`));
    } catch {
      throw new Error(
        `Cannot resolve CLI dependency ${JSON.stringify(name)} from this workspace; run "pnpm install" first.`,
      );
    }
    const tarball = await npmPack(sourceDir, outDir);
    log(`vendored dependency ${name}@${specs[name]} from ${sourceDir}`);
    tarballs.push(tarball);
  }
  return { specs, tarballs };
}

function hostRuntimePackage() {
  if (process.platform === "win32" && process.arch === "x64") {
    return { dir: join(WORKSPACE_ROOT, "packages", "core-win32-x64"), name: "@pablozrrrr/core-win32-x64" };
  }
  if (process.platform === "darwin" && process.arch === "x64") {
    return { dir: join(WORKSPACE_ROOT, "packages", "core-darwin-x64"), name: "@pablozrrrr/core-darwin-x64" };
  }
  if (process.platform === "darwin" && process.arch === "arm64") {
    return { dir: join(WORKSPACE_ROOT, "packages", "core-darwin-arm64"), name: "@pablozrrrr/core-darwin-arm64" };
  }
  if (process.platform === "linux" && process.arch === "x64") {
    return { dir: join(WORKSPACE_ROOT, "packages", "core-linux-x64-gnu"), name: "@pablozrrrr/core-linux-x64-gnu" };
  }
  throw new Error(`No local runtime package for ${process.platform}-${process.arch}. Pass --runtime-tarball explicitly.`);
}

/** The packed dist must never shell out to a toolchain binary. */
function assertNoToolchainCalls(distDir) {
  const pattern =
    /(spawn|spawnSync|execFile|execFileSync|exec|execSync)\s*\(\s*["'`]([^"'`]*\b(python\d?|pnpm|npx|\bng\b|tsc|ts-node|uv|pip)(\.cmd|\.exe|\.ps1)?\b[^"'`]*)/i;
  const offenders = [];
  const walk = (dir) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.isFile() && entry.name.endsWith(".js")) {
        const match = readFileSync(full, "utf8").match(pattern);
        if (match) offenders.push(`${full}: ${match[0].slice(0, 120)}`);
      }
    }
  };
  walk(distDir);
  if (offenders.length > 0) {
    throw new Error(`Packed CLI shells out to toolchain binaries:\n${offenders.join("\n")}`);
  }
  log("packed dist contains no python/pnpm/npx/ng/tsc spawn calls");
}

async function waitForStatus(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let lastError = "no attempts";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(STATUS_URL, { signal: AbortSignal.timeout(3000) });
      const body = await response.json();
      if (response.ok && body?.status === "ok" && body?.data?.system?.status) return body;
      lastError = `unexpected status body: ${JSON.stringify(body).slice(0, 200)}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolveSleep) => setTimeout(resolveSleep, POLL_INTERVAL_MS));
  }
  throw new Error(`Core did not become healthy at ${STATUS_URL} in ${timeoutMs}ms (last: ${lastError}).`);
}

function shutdownChild(child) {
  // Placeholder replaced below by the tree-aware shutdown helpers.
  return shutdownChildTree(child);
}

const SHUTDOWN_TERM_GRACE_MS = 15_000;
const SHUTDOWN_KILL_GRACE_MS = 10_000;
const SHUTDOWN_WINDOWS_GRACE_MS = 20_000;
const TASKKILL_TIMEOUT_MS = 15_000;

/** Children that provably emitted `close` (exit + stdio close). */
const closedChildren = new WeakSet();

/**
 * True once `close` provably fired. `destroyed` is deliberately excluded:
 * a destroyed pipe can precede the real close, so only the tracked `close`
 * event or `stream.closed` counts as proven.
 */
function isChildClosed(child) {
  if (closedChildren.has(child)) return true;
  const exited = child.exitCode !== null || child.signalCode !== null;
  const stdoutClosed = !child.stdout || child.stdout.closed === true;
  const stderrClosed = !child.stderr || child.stderr.closed === true;
  return Boolean(exited && stdoutClosed && stderrClosed);
}

/** Bounded wait for the child `close` event (exit + stdio close). Never hangs. */
function waitForClose(child, timeoutMs) {
  if (isChildClosed(child)) return Promise.resolve(true);
  return new Promise((resolve) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.off("close", onClose);
      resolve(isChildClosed(child));
    }, timeoutMs);
    const onClose = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      closedChildren.add(child);
      resolve(true);
    };
    child.once("close", onClose);
    // Close may have fired between the first check and listener registration.
    if (isChildClosed(child)) {
      settled = true;
      clearTimeout(timer);
      child.off("close", onClose);
      closedChildren.add(child);
      resolve(true);
    }
  });
}

function destroyChildPipes(child) {
  try {
    child.stdout?.destroy();
  } catch {
    // Best effort: unblock the event loop so the runner can exit and report.
  }
  try {
    child.stderr?.destroy();
  } catch {
    // Best effort: unblock the event loop so the runner can exit and report.
  }
}

/**
 * Terminate the exact tree owned by `pid` on Windows. Never matches by name;
 * only the recorded PID with /T (descendants). Resolves with the taskkill
 * outcome so the caller can accept benign not-found based on actual output.
 */
function taskkillTree(pid) {
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn("taskkill", ["/PID", String(pid), "/T", "/F"], { shell: false, windowsHide: true });
    } catch (error) {
      reject(error);
      return;
    }
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch {
        // Best effort; the close handler below reports the timeout.
      }
      reject(new Error(`taskkill /PID ${pid} /T /F timed out after ${TASKKILL_TIMEOUT_MS}ms.`));
    }, TASKKILL_TIMEOUT_MS);
    child.stdout?.on("data", (chunk) => {
      stdout += String(chunk);
    });
    child.stderr?.on("data", (chunk) => {
      stderr += String(chunk);
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr });
    });
  });
}

function isBenignTaskkillNotFound(outcome) {
  const output = `${outcome.stdout}\n${outcome.stderr}`;
  return /could not be found|not found|no such process|does not exist|no process/i.test(output);
}

async function shutdownPosixTree(child, pid) {
  // Waiter registered before the first signal to avoid missing `close`.
  const closeAfterTerm = waitForClose(child, SHUTDOWN_TERM_GRACE_MS);
  try {
    process.kill(-pid, "SIGTERM");
  } catch (error) {
    if (error?.code !== "ESRCH") throw error;
  }
  if (await closeAfterTerm) return;
  const closeAfterKill = waitForClose(child, SHUTDOWN_KILL_GRACE_MS);
  try {
    process.kill(-pid, "SIGKILL");
  } catch (error) {
    if (error?.code !== "ESRCH") throw error;
  }
  if (await closeAfterKill) return;
  destroyChildPipes(child);
  throw new Error(
    `Serve child PID ${pid} did not close after SIGKILL to its process group (platform ${process.platform}).`,
  );
}

async function shutdownWindowsTree(child, pid) {
  // Waiter registered before taskkill so a fast `close` is never missed.
  const closeWait = waitForClose(child, SHUTDOWN_WINDOWS_GRACE_MS);
  let outcome;
  try {
    outcome = await taskkillTree(pid);
  } catch (error) {
    await closeWait;
    destroyChildPipes(child);
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(`taskkill /PID ${pid} /T /F failed (platform win32): ${detail}`);
  }
  if (outcome.code !== 0 && !isBenignTaskkillNotFound(outcome)) {
    await closeWait;
    destroyChildPipes(child);
    const detail = `${outcome.stdout}\n${outcome.stderr}`.trim().slice(-2000);
    throw new Error(`taskkill /PID ${pid} /T /F failed with code ${outcome.code}: ${detail}`);
  }
  if (await closeWait) return;
  destroyChildPipes(child);
  throw new Error(`Serve child PID ${pid} did not close after taskkill /T /F (platform win32).`);
}

/**
 * Shut down the exact tree created for `serve`: POSIX signals the dedicated
 * process group, Windows taskkills the recorded PID subtree. Resolves only
 * after `close` (exit + stdio close) so inherited pipes cannot keep the
 * runner alive. Rejects with PID/platform when `close` never occurs.
 */
async function shutdownChildTree(child) {
  const pid = child.pid;
  if (pid === undefined) {
    throw new Error(`Cannot shut down serve child without a PID (platform ${process.platform}).`);
  }
  if (process.platform === "win32") return shutdownWindowsTree(child, pid);
  return shutdownPosixTree(child, pid);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const tmpRoot = mkdtempSync(join(tmpdir(), "crucible-packed-smoke-"));
  const prefix = join(tmpRoot, "prefix");
  const cache = join(tmpRoot, "npm-cache");
  const dataDir = join(tmpRoot, "data");
  const tarballDir = join(tmpRoot, "tarballs");
  mkdirSync(prefix, { recursive: true });
  mkdirSync(cache, { recursive: true });
  mkdirSync(dataDir, { recursive: true });
  writeFileSync(join(prefix, "package.json"), JSON.stringify({ name: "crucible-packed-smoke", private: true }));

  let serveChild;
  let failed = false;
  try {
    const hostRuntime = hostRuntimePackage();
    const cliTarball = args.cliTarball ? resolve(args.cliTarball) : await packCli(CLI_ROOT, tarballDir);
    const runtimeTarball = args.runtimeTarball ? resolve(args.runtimeTarball) : await npmPack(hostRuntime.dir, tarballDir);
    for (const [label, tarball] of [["CLI", cliTarball], ["runtime", runtimeTarball]]) {
      if (!existsSync(tarball)) throw new Error(`${label} tarball not found at ${tarball}.`);
    }
    assertCliTarballOptionalDepsExact(cliTarball);
    log(`CLI tarball: ${cliTarball}`);
    log(`runtime tarball: ${runtimeTarball}`);
    const { specs: depSpecs, tarballs: depTarballs } = await vendorWorkspaceDepTarballs(tarballDir);

    log("installing tarballs into an isolated prefix (offline, no scripts)");
    const install = await runCapture(
      process.execPath,
      [
        npmCliJs(),
        "install",
        "--offline",
        "--ignore-scripts",
        "--no-save",
        "--no-package-lock",
        "--no-audit",
        "--no-fund",
        "--cache",
        cache,
        runtimeTarball,
        cliTarball,
        ...depTarballs,
      ],
      { cwd: prefix, env: cleanNpmEnv(process.env) },
    );
    if (/npm warn/i.test(install.stderr)) log(`npm warnings:\n${install.stderr.trim()}`);

    const cliDir = join(prefix, "node_modules", "@pablozrrrr", "cli");
    const cliEntry = join(cliDir, "dist", "index.js");
    const cliPkgPath = join(cliDir, "package.json");
    if (!existsSync(cliEntry)) throw new Error(`Installed CLI entry missing at ${cliEntry}.`);
    const cliPkg = JSON.parse(readFileSync(cliPkgPath, "utf8"));
    log(`installed @pablozrrrr/cli ${cliPkg.version}`);
    for (const [name, spec] of Object.entries(depSpecs)) {
      if (cliPkg.dependencies?.[name] !== spec) {
        throw new Error(
          `Installed CLI manifest lost dependency ${name}@${spec} (packed tarball is missing the declaration).`,
        );
      }
    }
    if (Object.keys(depSpecs).length > 0) log(`installed CLI manifest preserves dependencies: ${Object.keys(depSpecs).join(", ")}`);

    // The resolver creates require() relative to the CLI itself, so the
    // runtime must be reachable as a sibling top-level package.
    const requireFromCli = createRequire(pathToFileURL(cliEntry).href);
    const runtimePkgPath = requireFromCli.resolve(`${hostRuntime.name}/package.json`);
    log(`CLI resolves ${hostRuntime.name} at ${runtimePkgPath}`);
    const runtimeDir = dirname(runtimePkgPath);
    if (!existsSync(join(runtimeDir, "runtime-manifest.json"))) {
      throw new Error(`Resolved runtime is missing runtime-manifest.json at ${runtimeDir}.`);
    }

    // Packed dashboard assets must ship inside the tarball.
    const dashboardDir = join(cliDir, "dashboard");
    if (!existsSync(join(dashboardDir, "index.html"))) {
      throw new Error(`Packed dashboard assets missing at ${dashboardDir} (expected index.html).`);
    }
    log("packed dashboard assets present (dashboard/index.html)");

    // Exercise the packed static handler directly: no browser, no persistence.
    const { createDashboardHandler } = await import(
      pathToFileURL(join(cliDir, "dist", "dashboard", "server.js")).href
    );
    const dashboardServer = createServer(createDashboardHandler(dashboardDir, "http://127.0.0.1:7331"));
    await new Promise((promiseResolve, promiseReject) => {
      dashboardServer.once("error", promiseReject);
      dashboardServer.listen(0, "127.0.0.1", () => promiseResolve());
    });
    try {
      const address = dashboardServer.address();
      if (!address || typeof address !== "object") throw new Error("Dashboard probe server has no address.");
      const probe = await fetch(`http://127.0.0.1:${address.port}/`, { signal: AbortSignal.timeout(5000) });
      const probeText = await probe.text();
      if (!probe.ok || !probeText.includes("<!doctype html")) {
        throw new Error(`Packed dashboard handler did not serve index.html (status ${probe.status}).`);
      }
      const v1 = await fetch(`http://127.0.0.1:${address.port}/v1/status`, {
        signal: AbortSignal.timeout(5000),
      });
      await v1.text();
      if (v1.status === 200) throw new Error("Packed dashboard handler must not SPA-fallback /v1 routes.");
      log("packed dashboard handler serves index.html and guards /v1 routes");
    } finally {
      await new Promise((promiseResolve) => dashboardServer.close(() => promiseResolve()));
    }

    assertNoToolchainCalls(join(cliDir, "dist"));

    log("running packed CLI --version");
    const { stdout: versionOut } = await runCapture(process.execPath, [cliEntry, "--version"], { cwd: tmpRoot });
    if (versionOut.trim() !== String(cliPkg.version)) {
      throw new Error(
        `--version mismatch: got ${JSON.stringify(versionOut.trim())}, want ${JSON.stringify(cliPkg.version)}.`,
      );
    }
    log(`packed CLI --version -> ${versionOut.trim()}`);

    log("starting packed CLI serve with a temp CRUCIBLE_DATA_DIR");
    let serveOutput = "";
    serveChild = spawn(process.execPath, [cliEntry, "serve"], {
      env: { ...process.env, CRUCIBLE_DATA_DIR: dataDir },
      stdio: ["ignore", "pipe", "pipe"],
      shell: false,
      detached: process.platform !== "win32",
      windowsHide: true,
    });
    serveChild.stdout?.on("data", (chunk) => {
      serveOutput += String(chunk);
    });
    serveChild.stderr?.on("data", (chunk) => {
      serveOutput += String(chunk);
    });
    const earlyExit = new Promise((_, rejectExit) => {
      serveChild?.once("exit", (code, signal) => {
        rejectExit(new Error(`serve exited early with code ${code} signal ${signal}. Output:\n${serveOutput.slice(-2000)}`));
      });
    });
    // Suppress unhandled rejection once the race has a winner.
    earlyExit.catch(() => {});

    const envelope = await Promise.race([waitForStatus(args.timeoutMs), earlyExit]);
    log(
      `core status ok (version ${envelope.data.system.version}, migration ${envelope.data.system.database.migration_revision})`,
    );

    await shutdownChild(serveChild);
    serveChild = undefined;
    log("serve shut down cleanly");

    // Confirm the data dir was actually used (proves the env contract).
    if (!existsSync(join(dataDir, "crucible.db"))) {
      throw new Error(`Expected a database at ${join(dataDir, "crucible.db")}; CRUCIBLE_DATA_DIR was not honored.`);
    }
    log("CRUCIBLE_DATA_DIR honored (crucible.db created)");

    log("PACKED SMOKE PASSED");
  } catch (error) {
    failed = true;
    throw error;
  } finally {
    // Any still-recorded child may have open pipes or live descendants even
    // when the launcher already exited, so always clean it up. Shutdown only
    // clears `serveChild` on proven `close`; silence the retry only when a
    // primary failure already determines the outcome.
    if (serveChild) {
      if (failed) {
        await shutdownChild(serveChild).catch(() => {});
      } else {
        await shutdownChild(serveChild);
      }
      serveChild = undefined;
    }
    if (!args.keepTemp || failed) {
      rmSync(tmpRoot, { recursive: true, force: true });
      if (!args.keepTemp) log(`removed temp dir ${tmpRoot}`);
    } else {
      log(`kept temp dir ${tmpRoot}`);
    }
  }
}

void main().catch((error) => {
  console.error(`[packed-smoke] FAILED: ${error instanceof Error ? error.stack ?? error.message : String(error)}`);
  process.exitCode = 1;
});
