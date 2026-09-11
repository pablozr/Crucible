import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { chmodSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  CORE_EXECUTABLE_ENV,
  resolveCore,
  resolveCoreExecutable,
} from "../src/runtime/resolve.js";
import { selectRuntimeTarget } from "../src/runtime/targets.js";
import { getCliProductVersion } from "../src/runtime/version.js";


function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

test("target matrix selects the expected optional package", () => {
  assert.deepEqual(selectRuntimeTarget({ platform: "win32", arch: "x64" }), {
    target: "win32-x64",
    packageName: "@crucible/core-win32-x64",
  });
  assert.deepEqual(selectRuntimeTarget({ platform: "darwin", arch: "x64" }), {
    target: "darwin-x64",
    packageName: "@crucible/core-darwin-x64",
  });
  assert.deepEqual(selectRuntimeTarget({ platform: "darwin", arch: "arm64" }), {
    target: "darwin-arm64",
    packageName: "@crucible/core-darwin-arm64",
  });
  assert.deepEqual(
    selectRuntimeTarget({ platform: "linux", arch: "x64", libc: "glibc" }),
    { target: "linux-x64-gnu", packageName: "@crucible/core-linux-x64-gnu" },
  );
});

test("target selection uses the injectable libc probe on linux", () => {
  assert.equal(
    selectRuntimeTarget({ platform: "linux", arch: "x64", probeLibc: () => "glibc" }).target,
    "linux-x64-gnu",
  );
  assert.throws(
    () => selectRuntimeTarget({ platform: "linux", arch: "x64", probeLibc: () => "unknown" }),
    /glibc/,
  );
});

test("target selection refuses musl and unknown libc (fail closed)", () => {
  assert.throws(
    () => selectRuntimeTarget({ platform: "linux", arch: "x64", libc: "musl" }),
    /musl.*glibc/i,
  );
  assert.throws(
    () => selectRuntimeTarget({ platform: "linux", arch: "x64", libc: "unknown" }),
    /glibc/,
  );
});

test("target selection rejects unsupported platform/arch with the supported list", () => {
  for (const [platform, arch] of [
    ["win32", "arm64"],
    ["linux", "arm64"],
    ["freebsd", "x64"],
  ] as const) {
    assert.throws(
      () => selectRuntimeTarget({ platform, arch }),
      (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.match(error.message, /Unsupported platform/);
        for (const target of ["win32-x64", "darwin-x64", "darwin-arm64", "linux-x64-gnu"]) {
          assert.ok(error.message.includes(target), `missing ${target}`);
        }
        return true;
      },
    );
  }
});

type FixtureOptions = {
  target?: string;
  executable?: string;
  sha256?: string;
  productVersion?: string;
  apiVersion?: string;
  schemaVersion?: number;
  rawManifest?: string;
  packageName?: string;
  exeBytes?: Uint8Array;
  mode?: number;
  /** When false, the manifest points at a file that is never written. */
  writeExe?: boolean;
};

function makePackageFixture(options: FixtureOptions = {}): {
  packageJsonPath: string;
  executablePath: string;
  packageName: string;
} {
  const target = options.target ?? "win32-x64";
  const packageName = options.packageName ?? `@crucible/core-${target}`;
  const dir = mkdtempSync(join(tmpdir(), "crucible-runtime-"));
  const pkgDir = join(dir, "pkg");
  const executableRel = options.executable ?? "bin/crucible-core.exe";
  const exeBytes = options.exeBytes ?? Buffer.from("fake-executable-bytes");
  const digest = options.sha256 ?? sha256(exeBytes);

  const manifest =
    options.rawManifest ??
    JSON.stringify({
      schemaVersion: options.schemaVersion ?? 1,
      productVersion: options.productVersion ?? getCliProductVersion(),
      apiVersion: options.apiVersion ?? "v1",
      target,
      executable: executableRel,
      sha256: digest,
    });

  mkdirSync(join(pkgDir, "bin"), { recursive: true });
  const packageJsonPath = join(pkgDir, "package.json");
  writeFileSync(packageJsonPath, JSON.stringify({ name: packageName }));
  writeFileSync(join(pkgDir, "runtime-manifest.json"), manifest);
  const executablePath = executableRel === ""
    ? join(pkgDir, "never-written")
    : join(pkgDir, ...executableRel.split("/"));
  if (executableRel !== "" && options.writeExe !== false) {
    mkdirSync(join(executablePath, ".."), { recursive: true });
    writeFileSync(executablePath, exeBytes);
    if (options.mode !== undefined) {
      chmodSync(executablePath, options.mode);
    }
  }

  return { packageJsonPath, executablePath, packageName };
}

function platformArchFor(target: string): { platform: NodeJS.Platform; arch: NodeJS.Architecture } {
  if (target === "win32-x64") return { platform: "win32", arch: "x64" };
  if (target === "darwin-x64") return { platform: "darwin", arch: "x64" };
  if (target === "darwin-arm64") return { platform: "darwin", arch: "arm64" };
  return { platform: "linux", arch: "x64" };
}

test("resolves a temp package and verifies the executable hash", () => {
  const fixture = makePackageFixture();
  const resolved = resolveCore({
    ...platformArchFor("win32-x64"),
    requireResolve: (spec) => {
      assert.equal(spec, "@crucible/core-win32-x64/package.json");
      return fixture.packageJsonPath;
    },
  });

  assert.equal(resolved.executablePath, fixture.executablePath);
  assert.equal(resolved.packageName, "@crucible/core-win32-x64");
  assert.equal(resolveCoreExecutable({
    ...platformArchFor("win32-x64"),
    requireResolve: () => fixture.packageJsonPath,
  }), fixture.executablePath);
});

test("missing optional package names --omit=optional recovery", () => {
  let message = "";
  try {
    resolveCore({
      ...platformArchFor("win32-x64"),
      requireResolve: () => {
        const missing = new Error("Cannot find module") as NodeJS.ErrnoException;
        missing.code = "MODULE_NOT_FOUND";
        throw missing;
      },
    });
  } catch (error) {
    assert.ok(error instanceof Error);
    message = error.message;
  }
  assert.match(message, /optional package @crucible\/core-win32-x64/);
  assert.match(message, /--omit=optional/);
  assert.match(message, /npm install -g @crucible\/cli/);
});

test("malformed manifest JSON is rejected", () => {
  const fixture = makePackageFixture({ rawManifest: "{not-json" });
  assert.throws(
    () =>
      resolveCore({
        ...platformArchFor("win32-x64"),
        requireResolve: () => fixture.packageJsonPath,
      }),
    /not valid JSON/,
  );
});

test("manifest schema/api/product/target mismatches are rejected", () => {
  const cases: Array<[string, FixtureOptions, RegExp]> = [
    ["schema", { schemaVersion: 2 }, /schemaVersion/],
    ["api", { apiVersion: "v2" }, /apiVersion/],
    ["product", { productVersion: "9.9.9" }, /productVersion mismatch/],
    ["target", { target: "darwin-arm64" }, /target mismatch/],
    ["sha format", { sha256: "xyz" }, /sha256/],
  ];
  for (const [label, options, pattern] of cases) {
    const fixture = makePackageFixture(options);
    assert.throws(
      () =>
        resolveCore({
          ...platformArchFor("win32-x64"),
          requireResolve: () => fixture.packageJsonPath,
        }),
      pattern,
      label,
    );
  }
});

test("manifest executable traversal and absolute paths are rejected", () => {
  for (const executable of ["../evil.exe", "/abs/evil", "bin\\evil.exe", "bin/./x", "bin//x", ""]) {
    const fixture = makePackageFixture({ executable: executable === "" ? "" : executable });
    assert.throws(
      () =>
        resolveCore({
          ...platformArchFor("win32-x64"),
          requireResolve: () => fixture.packageJsonPath,
        }),
      /executable/i,
      executable === "" ? "(empty)" : executable,
    );
  }
});

test("executable hash mismatch is rejected", () => {
  const fixture = makePackageFixture({ sha256: "0".repeat(64) });
  assert.throws(
    () =>
      resolveCore({
        ...platformArchFor("win32-x64"),
        requireResolve: () => fixture.packageJsonPath,
      }),
    /integrity check.*sha256 mismatch/,
  );
});

test("missing executable file is rejected", () => {
  // Point the manifest at a file that was never written.
  const fixture = makePackageFixture({ executable: "bin/never-built.exe", writeExe: false });
  assert.throws(
    () =>
      resolveCore({
        ...platformArchFor("win32-x64"),
        requireResolve: () => fixture.packageJsonPath,
      }),
    /missing at/,
  );
});

test("non-executable bit is rejected on posix targets", () => {
  const fixture = makePackageFixture({
    target: "linux-x64-gnu",
    executable: "bin/crucible-core",
    mode: 0o644,
  });
  assert.throws(
    () =>
      resolveCore({
        platform: "linux",
        arch: "x64",
        libc: "glibc",
        requireResolve: () => fixture.packageJsonPath,
        statFile: () => ({ isFile: () => true, mode: 0o644 }),
      }),
    /not executable/,
  );
});

test("env override accepts an absolute file without hash validation", () => {
  const dir = mkdtempSync(join(tmpdir(), "crucible-override-"));
  const override = join(dir, "core-dev");
  writeFileSync(override, "dev-bytes");
  // No requireResolve: the override must not touch package resolution.
  const resolved = resolveCore({
    platform: "win32",
    arch: "x64",
    env: { [CORE_EXECUTABLE_ENV]: override },
    requireResolve: () => {
      throw new Error("must not resolve packages for the override");
    },
  });
  assert.equal(resolved.executablePath, override);
});

test("env override rejects relative and missing paths", () => {
  assert.throws(
    () => resolveCoreExecutable({ env: { [CORE_EXECUTABLE_ENV]: "relative/bin" } }),
    /absolute/,
  );
  const missing = join(mkdtempSync(join(tmpdir(), "crucible-override-missing-")), "nope");
  assert.throws(
    () => resolveCoreExecutable({ env: { [CORE_EXECUTABLE_ENV]: missing } }),
    /missing file/,
  );
});

test("cli product version matches the published package version", () => {
  assert.equal(getCliProductVersion(), "0.1.0");
});
