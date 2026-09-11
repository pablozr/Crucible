// Release contract check for the Crucible 0.1.0 prerelease line.
// Stdlib-only: `node scripts/verify-release.mjs [--expected-version X]
// [--require-runtime-manifests] [--runtime-package <dir>
// [--require-runtime-manifest]] [--tarball-dir <dir>]`.
//
// Three modes (source metadata is validated in all of them):
// - default: validates the single exact product version shared by core
//   pyproject project.version, core src version.py, the CLI package, the CLI
//   optionalDependencies pins, and the four runtime npm manifests. Also
//   validates the CLI runtime fallback constant, runtime target/os/cpu/libc
//   metadata, publish metadata, tarball `files` entries, and — when present —
//   the built runtime-manifest.json artifacts (absent ones are skipped unless
//   --require-runtime-manifests is passed). Used pre-build in build-cli, where
//   no runtime manifests exist yet.
// - --runtime-package <dir>: same source checks, but target/publish/manifest
//   checks are restricted to the single runtime package in <dir> (e.g.
//   packages/core-win32-x64). The generated runtime-manifest.json is strictly
//   required and the real executable file on disk is hashed and compared
//   against it. Used in each build-runtime matrix job, whose checkout only
//   contains the manifest of its own target.
// - --tarball-dir <dir>: same source checks (other-OS manifests absent, so
//   only warned), plus strict aggregate-layout checks on <dir>: exactly the 5
//   expected tarballs for the product version, a SHA256SUMS file whose
//   entries match the recomputed file hashes, and an inspection of the packed
//   CLI manifest proving the package name is @pablozrrrr/cli and the
//   `workspace:` source pins were converted to exact optionalDependencies
//   pins. NOTE: this mode checks file names, hashes, and
//   the packed CLI manifest only; other package
//   contents were already validated by the --runtime-package checks in the
//   per-OS jobs and the CLI source check in build-cli.
import { closeSync, openSync, readdirSync, readFileSync, readSync, statSync } from "node:fs";
import { gunzipSync } from "node:zlib";
import { createHash } from "node:crypto";
import { dirname, isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const REPO_URL = "git+https://github.com/pablozr/Crucible.git";
const CLI_PACKAGE_NAME = "@pablozrrrr/cli";

const RUNTIMES = [
  {
    target: "win32-x64",
    packageName: "@pablozrrrr/core-win32-x64",
    dir: "packages/core-win32-x64",
    os: ["win32"],
    cpu: ["x64"],
    executable: "bin/crucible-core.exe",
  },
  {
    target: "darwin-x64",
    packageName: "@pablozrrrr/core-darwin-x64",
    dir: "packages/core-darwin-x64",
    os: ["darwin"],
    cpu: ["x64"],
    executable: "bin/crucible-core",
  },
  {
    target: "darwin-arm64",
    packageName: "@pablozrrrr/core-darwin-arm64",
    dir: "packages/core-darwin-arm64",
    os: ["darwin"],
    cpu: ["arm64"],
    executable: "bin/crucible-core",
  },
  {
    target: "linux-x64-gnu",
    packageName: "@pablozrrrr/core-linux-x64-gnu",
    dir: "packages/core-linux-x64-gnu",
    os: ["linux"],
    cpu: ["x64"],
    libc: ["glibc"],
    executable: "bin/crucible-core",
  },
];

const errors = [];
const warnings = [];
const fail = (message) => errors.push(message);
const warn = (message) => warnings.push(message);

function readText(relPath) {
  try {
    return readFileSync(join(ROOT, relPath), "utf8");
  } catch {
    fail(`${relPath}: file not found or unreadable`);
    return undefined;
  }
}

function readJson(relPath) {
  const text = readText(relPath);
  if (text === undefined) return undefined;
  try {
    return JSON.parse(text);
  } catch (error) {
    fail(`${relPath}: invalid JSON (${error.message})`);
    return undefined;
  }
}

function failValue(relPath, field, actual, expected) {
  fail(`${relPath}: ${field} is ${JSON.stringify(actual)} (expected ${JSON.stringify(expected)})`);
}

function checkStringArray(relPath, field, actual, expected) {
  if (
    !Array.isArray(actual) ||
    actual.length !== expected.length ||
    !expected.every((value, index) => actual[index] === value)
  ) {
    failValue(relPath, field, actual, expected);
  }
}

// --- CLI args -------------------------------------------------------------
const USAGE = "verify-release.mjs [--expected-version X] [--require-runtime-manifests] [--runtime-package <dir> [--require-runtime-manifest]] [--tarball-dir <dir>]";
const args = process.argv.slice(2);
let expectedVersion;
let requireRuntimeManifests = false;
let runtimePackageDir;
let tarballDir;
for (let i = 0; i < args.length; i += 1) {
  if (args[i] === "--expected-version") {
    expectedVersion = args[i + 1];
    i += 1;
  } else if (args[i] === "--require-runtime-manifests" || args[i] === "--require-runtime-manifest") {
    requireRuntimeManifests = true;
  } else if (args[i] === "--runtime-package") {
    runtimePackageDir = args[i + 1];
    i += 1;
  } else if (args[i] === "--tarball-dir") {
    tarballDir = args[i + 1];
    i += 1;
  } else {
    fail(`unknown argument: ${args[i]} (usage: ${USAGE})`);
  }
}
if (runtimePackageDir !== undefined && tarballDir !== undefined) {
  fail("--runtime-package and --tarball-dir are mutually exclusive");
}
const normDir = (value) => value.replace(/\\/g, "/").replace(/\/+$/, "");
let singleRuntime;
if (runtimePackageDir !== undefined) {
  singleRuntime = RUNTIMES.find((runtime) => runtime.dir === normDir(runtimePackageDir));
  if (singleRuntime === undefined) {
    fail(`--runtime-package: unknown package dir ${JSON.stringify(runtimePackageDir)} (expected one of: ${RUNTIMES.map((runtime) => runtime.dir).join(", ")})`);
  }
}
// In --runtime-package mode only the targeted runtime is checked (the matrix
// job's checkout only has its own built manifest); otherwise all four are.
const checkedRuntimes = singleRuntime !== undefined ? [singleRuntime] : RUNTIMES;
// In --runtime-package mode the built manifest + executable are always
// required, with or without the explicit flag.
const strictRuntimeArtifact = singleRuntime !== undefined;

// --- Core versions --------------------------------------------------------
let corePyprojectVersion;
const pyproject = readText("core/pyproject.toml");
if (pyproject !== undefined) {
  const projectSection = pyproject.split(/^\[[^\]]+\]/m)[1] ?? "";
  const match = projectSection.match(/^\s*version\s*=\s*"([^"]+)"\s*$/m);
  if (match) {
    corePyprojectVersion = match[1].trim();
  } else {
    fail("core/pyproject.toml: cannot parse project.version from the [project] section");
  }
}

let coreSrcVersion;
const versionPy = readText("core/src/crucible_core/version.py");
if (versionPy !== undefined) {
  const match = versionPy.match(/^VERSION\s*=\s*["']([^"']+)["']/m);
  if (match) {
    coreSrcVersion = match[1].trim();
  } else {
    fail("core/src/crucible_core/version.py: cannot parse VERSION");
  }
}

// --- CLI package ----------------------------------------------------------
const cliPkg = readJson("packages/cli/package.json");
let cliVersion;
if (cliPkg !== undefined) {
  cliVersion = cliPkg.version;
  if (cliPkg.name !== CLI_PACKAGE_NAME) {
    failValue("packages/cli/package.json", "name", cliPkg.name, CLI_PACKAGE_NAME);
  }
  // Source may pin workspace-local runtimes via the workspace: protocol
  // (pnpm resolves it to a local link). Published consumers must receive the
  // exact version instead — enforced on the packed CLI tarball in
  // --tarball-dir mode below. `workspace:*` / `workspace:^` are rejected so
  // the source pin stays exact.
  for (const runtime of RUNTIMES) {
    const pinned = cliPkg.optionalDependencies?.[runtime.packageName];
    const allowed = cliVersion === undefined ? [] : [cliVersion, `workspace:${cliVersion}`];
    if (!allowed.includes(pinned)) {
      failValue(
        "packages/cli/package.json",
        `optionalDependencies["${runtime.packageName}"]`,
        pinned,
        cliVersion,
      );
    } else if (typeof pinned === "string" && pinned.startsWith("workspace:")) {
      warn(
        `packages/cli/package.json: optionalDependencies["${runtime.packageName}"] uses ${JSON.stringify(pinned)} in source; publish check requires the packed tarball to contain ${JSON.stringify(cliVersion)}`,
      );
    }
  }
  for (const entry of ["dist/**", "README.md", "LICENSE"]) {
    if (!Array.isArray(cliPkg.files) || !cliPkg.files.includes(entry)) {
      fail(`packages/cli/package.json: files[] is missing ${JSON.stringify(entry)} (tarball must ship dist, README, and LICENSE)`);
    }
  }
  for (const doc of ["packages/cli/README.md", "packages/cli/LICENSE"]) {
    if (readText(doc) === undefined) {
      fail(`${doc}: missing (packages/cli/package.json files[] ships it)`);
    }
  }
}

// --- CLI runtime fallback -------------------------------------------------
const versionTs = readText("packages/cli/src/runtime/version.ts");
if (versionTs !== undefined) {
  const match = versionTs.match(/CLI_PRODUCT_VERSION_FALLBACK\s*=\s*"([^"]+)"/);
  if (match === null) {
    warn("packages/cli/src/runtime/version.ts: no CLI_PRODUCT_VERSION_FALLBACK constant found; skipping fallback check");
  } else if (cliVersion !== undefined && match[1] !== cliVersion) {
    failValue(
      "packages/cli/src/runtime/version.ts",
      "CLI_PRODUCT_VERSION_FALLBACK",
      match[1],
      cliVersion,
    );
  }
}

// --- Single product version ----------------------------------------------
const productVersion = corePyprojectVersion ?? coreSrcVersion ?? cliVersion;
if (corePyprojectVersion !== undefined && coreSrcVersion !== undefined && corePyprojectVersion !== coreSrcVersion) {
  fail(`product version mismatch: core/pyproject.toml has ${JSON.stringify(corePyprojectVersion)} but core/src/crucible_core/version.py has ${JSON.stringify(coreSrcVersion)}`);
}
if (cliVersion !== undefined && productVersion !== undefined && cliVersion !== productVersion) {
  fail(`product version mismatch: packages/cli/package.json has ${JSON.stringify(cliVersion)} but product version is ${JSON.stringify(productVersion)}`);
}
if (expectedVersion !== undefined && productVersion !== undefined && productVersion !== expectedVersion) {
  fail(`product version mismatch: found ${JSON.stringify(productVersion)} but --expected-version is ${JSON.stringify(expectedVersion)}`);
}

// --- Publish metadata (CLI + runtimes) ------------------------------------
const releasePackages = [
  { relPath: "packages/cli/package.json", pkg: cliPkg, directory: "packages/cli" },
];
for (const runtime of checkedRuntimes) {
  const relPath = `${runtime.dir}/package.json`;
  releasePackages.push({ relPath, pkg: readJson(relPath), directory: runtime.dir });
}

for (const { relPath, pkg, directory } of releasePackages) {
  if (pkg === undefined) continue;
  if (pkg.version !== productVersion) {
    failValue(relPath, "version", pkg.version, productVersion);
  }
  if (typeof pkg.description !== "string" || pkg.description.length === 0) {
    fail(`${relPath}: description is missing or empty`);
  }
  if (pkg.license !== "MIT") {
    failValue(relPath, "license", pkg.license, "MIT");
  }
  if (pkg.publishConfig?.access !== "public") {
    failValue(relPath, "publishConfig.access", pkg.publishConfig?.access, "public");
  }
  if (pkg.repository?.url !== REPO_URL) {
    failValue(relPath, "repository.url", pkg.repository?.url, REPO_URL);
  }
  if (pkg.repository?.directory !== directory) {
    failValue(relPath, "repository.directory", pkg.repository?.directory, directory);
  }
}

// --- Runtime target metadata ----------------------------------------------
for (const runtime of checkedRuntimes) {
  const relPath = `${runtime.dir}/package.json`;
  const pkg = releasePackages.find((entry) => entry.relPath === relPath)?.pkg;
  if (pkg === undefined) continue;
  if (pkg.name !== runtime.packageName) {
    failValue(relPath, "name", pkg.name, runtime.packageName);
  }
  checkStringArray(relPath, "os", pkg.os, runtime.os);
  checkStringArray(relPath, "cpu", pkg.cpu, runtime.cpu);
  if (runtime.libc !== undefined) {
    checkStringArray(relPath, "libc", pkg.libc, runtime.libc);
  } else if (pkg.libc !== undefined) {
    failValue(relPath, "libc", pkg.libc, undefined);
  }
  for (const entry of ["bin/**", "runtime-manifest.json", "LICENSE"]) {
    if (!Array.isArray(pkg.files) || !pkg.files.includes(entry)) {
      fail(`${relPath}: files[] is missing ${JSON.stringify(entry)} (tarball must ship bin, runtime-manifest.json, and LICENSE)`);
    }
  }
}

// --- Built runtime-manifest.json artifacts (when present) ------------------
const SHA256_HEX = /^[0-9a-f]{64}$/;

function sha256File(absPath) {
  const fd = openSync(absPath, "r");
  try {
    const hash = createHash("sha256");
    const buffer = Buffer.alloc(1024 * 1024);
    let read;
    while ((read = readSync(fd, buffer, 0, buffer.length, null)) > 0) {
      hash.update(buffer.subarray(0, read));
    }
    return hash.digest("hex");
  } finally {
    closeSync(fd);
  }
}

function checkBuiltManifest(runtime, strict) {
  const relPath = `${runtime.dir}/runtime-manifest.json`;
  let text;
  try {
    text = readFileSync(join(ROOT, relPath), "utf8");
  } catch {
    if (strict) {
      fail(`${relPath}: not present (required in --runtime-package mode; build the runtime first)`);
    } else {
      const message = `${relPath}: not present (built in CI); skipping artifact check`;
      if (requireRuntimeManifests) fail(message);
      else warn(message);
    }
    return;
  }
  let manifest;
  try {
    manifest = JSON.parse(text);
  } catch (error) {
    fail(`${relPath}: invalid JSON (${error.message})`);
    return;
  }
  if (manifest.schemaVersion !== 1) failValue(relPath, "schemaVersion", manifest.schemaVersion, 1);
  if (manifest.apiVersion !== "v1") failValue(relPath, "apiVersion", manifest.apiVersion, "v1");
  if (manifest.target !== runtime.target) failValue(relPath, "target", manifest.target, runtime.target);
  if (manifest.productVersion !== productVersion) {
    failValue(relPath, "productVersion", manifest.productVersion, productVersion);
  }
  if (manifest.executable !== runtime.executable) {
    failValue(relPath, "executable", manifest.executable, runtime.executable);
  }
  if (typeof manifest.sha256 !== "string" || !SHA256_HEX.test(manifest.sha256)) {
    fail(`${relPath}: sha256 must be 64 lowercase hex characters`);
    return;
  }
  if (!strict) return;
  // Strict per-runtime check: the executable on disk must exist and hash to
  // the manifest digest, so a stale manifest cannot pass.
  const exeRelPath = `${runtime.dir}/${runtime.executable}`;
  let stat;
  try {
    stat = statSync(join(ROOT, exeRelPath));
  } catch {
    fail(`${exeRelPath}: executable not found (expected by ${relPath})`);
    return;
  }
  if (!stat.isFile()) {
    fail(`${exeRelPath}: not a file (expected by ${relPath})`);
    return;
  }
  const actual = sha256File(join(ROOT, exeRelPath));
  if (actual !== manifest.sha256) {
    fail(`${exeRelPath}: sha256 ${JSON.stringify(actual)} does not match ${relPath} sha256 ${JSON.stringify(manifest.sha256)}`);
  }
}

for (const runtime of checkedRuntimes) {
  checkBuiltManifest(runtime, strictRuntimeArtifact);
}

// --- Aggregate tarball dir (--tarball-dir) ---------------------------------
// Strict layout check: exact tarball filenames for the product version plus a
// SHA256SUMS file whose entries match the recomputed file hashes. In
// addition, the packed CLI tarball's embedded package/package.json is
// inspected: its optionalDependencies must be the exact product version with
// no `workspace:` protocol left — pack/publish must have converted the
// workspace: source pins. Tarball contents other than this manifest were
// validated by the CLI source check and the per-OS --runtime-package checks
// before packing.
function expectedTarballNames(version) {
  return [
    `crucible-cli-${version}.tgz`,
    ...RUNTIMES.map((runtime) => `crucible-core-${runtime.target}-${version}.tgz`),
  ];
}

// Read a single file out of a .tgz without shelling out (stdlib-only:
// gunzip + minimal ustar scan for the exact entry path).
function readPackedJson(absTgzPath, innerPath) {
  let archive;
  try {
    archive = gunzipSync(readFileSync(absTgzPath));
  } catch {
    return undefined;
  }
  let offset = 0;
  while (offset + 512 <= archive.length) {
    const header = archive.subarray(offset, offset + 512);
    if (header.every((byte) => byte === 0)) break;
    const name = header.subarray(0, 100).toString("utf8").replace(/\0.*$/, "");
    const prefix = header.subarray(345, 500).toString("utf8").replace(/\0.*$/, "");
    const fullName = prefix ? `${prefix}/${name}` : name;
    const sizeText = header.subarray(124, 136).toString("utf8").replace(/\0.*$/, "").trim();
    const size = sizeText ? Number.parseInt(sizeText, 8) : 0;
    const dataStart = offset + 512;
    const dataEnd = dataStart + (Number.isSafeInteger(size) ? size : 0);
    if (fullName === innerPath) {
      try {
        return JSON.parse(archive.subarray(dataStart, dataEnd).toString("utf8"));
      } catch {
        return undefined;
      }
    }
    offset = dataStart + Math.ceil((Number.isSafeInteger(size) ? size : 0) / 512) * 512;
  }
  return undefined;
}

if (tarballDir !== undefined) {
  const version = expectedVersion ?? productVersion;
  if (version === undefined) {
    fail("--tarball-dir: cannot determine product version from source metadata");
  } else {
    const absDir = isAbsolute(tarballDir) ? tarballDir : join(ROOT, tarballDir);
    let entries;
    try {
      if (!statSync(absDir).isDirectory()) {
        fail(`${tarballDir}: not a directory`);
        entries = undefined;
      } else {
        entries = readdirSync(absDir);
      }
    } catch {
      fail(`${tarballDir}: directory not found or unreadable`);
      entries = undefined;
    }
    if (entries !== undefined) {
      const expected = expectedTarballNames(version).slice().sort();
      const actual = entries.filter((entry) => entry.endsWith(".tgz")).sort();
      for (const name of expected) {
        if (!actual.includes(name)) {
          fail(`${tarballDir}: missing expected tarball ${JSON.stringify(name)}`);
        }
      }
      for (const name of actual) {
        if (!expected.includes(name)) {
          fail(`${tarballDir}: unexpected tarball ${JSON.stringify(name)} (expected exactly: ${expected.join(", ")})`);
        }
      }
      const sumsText = (() => {
        try {
          return readFileSync(join(absDir, "SHA256SUMS"), "utf8");
        } catch {
          fail(`${tarballDir}/SHA256SUMS: file not found or unreadable`);
          return undefined;
        }
      })();
      if (sumsText !== undefined) {
        const lines = sumsText.split("\n").filter((line) => line.length > 0);
        if (lines.length !== expected.length) {
          fail(`${tarballDir}/SHA256SUMS: has ${lines.length} entr${lines.length === 1 ? "y" : "ies"} (expected ${expected.length})`);
        }
        const seen = new Set();
        for (const line of lines) {
          const match = line.match(/^([0-9a-f]{64}) [ *](\S+)$/);
          if (match === null) {
            fail(`${tarballDir}/SHA256SUMS: malformed line ${JSON.stringify(line)} (expected "<64 hex>  <filename>")`);
            continue;
          }
          const [, recorded, rawName] = match;
          // `sha256sum ./*.tgz` (see package.yml) emits a benign `./` prefix
          // (e.g. `./crucible-cli-0.1.0.tgz`). Strip exactly one leading `./`
          // before comparison; anything else (nested paths, traversal,
          // absolutes, repeated prefixes) still fails the expected-name check.
          const name = rawName.startsWith("./") ? rawName.slice(2) : rawName;
          if (seen.has(name)) {
            fail(`${tarballDir}/SHA256SUMS: duplicate entry for ${JSON.stringify(name)}`);
            continue;
          }
          seen.add(name);
          if (!expected.includes(name)) {
            fail(`${tarballDir}/SHA256SUMS: unexpected filename ${JSON.stringify(name)}`);
            continue;
          }
          if (!actual.includes(name)) continue; // already reported as missing above
          let digest;
          try {
            digest = sha256File(join(absDir, name));
          } catch {
            fail(`${tarballDir}/${name}: file not found or unreadable`);
            continue;
          }
          if (digest !== recorded) {
            fail(`${tarballDir}/${name}: sha256 ${JSON.stringify(digest)} does not match SHA256SUMS entry ${JSON.stringify(recorded)}`);
          }
        }
      }
      // Publish conversion check: the packed CLI manifest must carry exact
      // optionalDependencies pins (no `workspace:` protocol survives packing).
      const cliTarball = `crucible-cli-${version}.tgz`;
      if (actual.includes(cliTarball)) {
        const packed = readPackedJson(join(absDir, cliTarball), "package/package.json");
        if (packed === undefined) {
          fail(`${tarballDir}/${cliTarball}: cannot read embedded package/package.json (pack the CLI with npm pack)`);
        } else {
          if (packed.name !== CLI_PACKAGE_NAME) {
            fail(
              `${tarballDir}/${cliTarball}: embedded package/package.json name is ${JSON.stringify(packed.name)} (expected ${JSON.stringify(CLI_PACKAGE_NAME)})`,
            );
          }
          for (const runtime of RUNTIMES) {
            const pinned = packed.optionalDependencies?.[runtime.packageName];
            if (pinned !== version) {
              fail(
                `${tarballDir}/${cliTarball}: embedded package/package.json optionalDependencies[${JSON.stringify(runtime.packageName)}] is ${JSON.stringify(pinned)} (expected exact ${JSON.stringify(version)}; workspace: protocol must be converted on pack)`,
              );
            }
          }
        }
      }
    }
  }
}

// --- Report ---------------------------------------------------------------
for (const message of warnings) console.warn(`warning: ${message}`);
if (errors.length > 0) {
  for (const message of errors) console.error(`error: ${message}`);
  console.error(`verify-release: FAILED with ${errors.length} error(s)`);
  process.exit(1);
}
console.log(`verify-release: OK (product version ${productVersion})`);
