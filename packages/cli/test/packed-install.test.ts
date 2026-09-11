import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import test from "node:test";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const execFileAsync = promisify(execFile);
const cliRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const smokeScript = join(cliRoot, "scripts", "smoke-packed-install.mjs");

// Opt-in only: the packed smoke packs tarballs, installs them into an
// isolated prefix, and boots the real frozen core binary, so it must never
// run as part of the default unit suite (`npm test` picks up this file via
// test/**/*.test.ts and skips it in <1s). Enable with:
//   CRUCIBLE_PACKED_SMOKE=1 tsx --test test/packed-install.test.ts
// or the explicit command (no env needed):
//   npm run test:packed
// Optional overrides: CRUCIBLE_CLI_TARBALL / CRUCIBLE_RUNTIME_TARBALL.
const enabled = process.env["CRUCIBLE_PACKED_SMOKE"] === "1";
const supported =
  process.env["CRUCIBLE_RUNTIME_TARBALL"] !== undefined ||
  (process.platform === "win32" && process.arch === "x64") ||
  (process.platform === "darwin" && (process.arch === "x64" || process.arch === "arm64")) ||
  (process.platform === "linux" && process.arch === "x64");

test("packed install smoke (opt-in)", { skip: !enabled, timeout: 600_000 }, async (t) => {
  if (!supported) {
    t.skip(`no local runtime package for ${process.platform}-${process.arch} (set CRUCIBLE_RUNTIME_TARBALL)`);
    return;
  }
  const args = [smokeScript];
  const cliTarball = process.env["CRUCIBLE_CLI_TARBALL"];
  const runtimeTarball = process.env["CRUCIBLE_RUNTIME_TARBALL"];
  if (cliTarball && runtimeTarball) args.push("--cli-tarball", cliTarball, "--runtime-tarball", runtimeTarball);
  else if (cliTarball || runtimeTarball) {
    throw new Error("Set both CRUCIBLE_CLI_TARBALL and CRUCIBLE_RUNTIME_TARBALL, or neither to pack locally.");
  }

  let output = "";
  try {
    const { stdout, stderr } = await execFileAsync(process.execPath, args, {
      cwd: cliRoot,
      shell: false,
      timeout: 540_000,
      maxBuffer: 16 * 1024 * 1024,
    });
    output = `${stdout}\n${stderr}`;
  } catch (error) {
    const stdout = (error as { stdout?: unknown }).stdout;
    const stderr = (error as { stderr?: unknown }).stderr;
    output = `${String(stdout ?? "")}\n${String(stderr ?? "")}`;
    console.error(output);
    throw error;
  }
  assert.match(output, /PACKED SMOKE PASSED/);
});
