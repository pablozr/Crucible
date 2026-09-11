import { createHash } from "node:crypto";
import { readFileSync as nodeReadFileSync, statSync as nodeStatSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";

import { validateRuntimeManifest, type RuntimeManifest } from "./manifest.js";
import { selectRuntimeTarget, type TargetSelectionDeps } from "./targets.js";
import { getCliProductVersion } from "./version.js";

export type { RuntimeManifest };
export { getCliProductVersion };

/** Explicit dev/source override. Absolute file path only; never PATH lookup. */
export const CORE_EXECUTABLE_ENV = "CRUCIBLE_CORE_EXECUTABLE";

export type ResolvedCore = {
  packageName: string;
  packageDir: string;
  executablePath: string;
  /** Absent when resolved via the developer override (no manifest involved). */
  manifest?: RuntimeManifest;
};

export type ResolveCoreDeps = TargetSelectionDeps & {
  env?: NodeJS.ProcessEnv;
  /** Base module URL used to resolve the optional package from the CLI itself. */
  fromUrl?: string;
  /** Resolve `<package>/package.json` to an absolute file path. */
  requireResolve?: (specifier: string) => string;
  readFile?: (path: string) => Buffer;
  statFile?: (path: string) => { isFile(): boolean; mode: number };
  hashBytes?: (bytes: Uint8Array) => string;
  /** Skip reading packages/cli/package.json (tests pin the version). */
  cliVersion?: string;
};

export type CoreExecutableDeps = Pick<
  ResolveCoreDeps,
  | "platform"
  | "arch"
  | "libc"
  | "probeLibc"
  | "env"
  | "fromUrl"
  | "requireResolve"
  | "readFile"
  | "statFile"
  | "hashBytes"
  | "cliVersion"
>;

function defaultRequireResolve(fromUrl: string): (specifier: string) => string {
  const require = createRequire(fromUrl);
  return (specifier: string) => require.resolve(specifier);
}

function defaultHashBytes(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function readJsonFile(readFile: (path: string) => Buffer, path: string): unknown {
  let bytes: Buffer;
  try {
    bytes = readFile(path);
  } catch {
    throw new Error(
      `Core runtime manifest is missing at ${path}. Reinstall the CLI without --omit=optional.`,
    );
  }
  try {
    return JSON.parse(bytes.toString("utf8"));
  } catch {
    throw new Error(`Core runtime manifest at ${path} is not valid JSON.`);
  }
}

/**
 * Resolve the bundled core executable for this host.
 *
 * `CRUCIBLE_CORE_EXECUTABLE` (absolute path to an existing file) wins for
 * tests/source checkouts and skips hash validation. Otherwise the optional
 * platform package is located from the CLI package itself, its
 * runtime-manifest.json is validated, and the executable's sha256 is
 * verified before returning. Never falls back to PATH.
 */
export function resolveCoreExecutable(deps: CoreExecutableDeps = {}): string {
  return resolveCore(deps).executablePath;
}

export function resolveCore(deps: CoreExecutableDeps = {}): ResolvedCore {
  const env = deps.env ?? process.env;
  const override = env[CORE_EXECUTABLE_ENV];
  if (override !== undefined && override !== "") {
    return resolveOverride(override, deps);
  }

  const { target, packageName } = selectRuntimeTarget(deps);
  const requireResolve =
    deps.requireResolve ?? defaultRequireResolve(deps.fromUrl ?? import.meta.url);

  let packageJsonPath: string;
  try {
    packageJsonPath = requireResolve(`${packageName}/package.json`);
  } catch {
    throw new Error(
      `Could not locate optional package ${packageName} for target ${target}. ` +
        `The CLI was likely installed with optional dependencies omitted. ` +
        `Reinstall with "npm install -g @pablozrrrr/crucible-cli" (do not pass --omit=optional).`,
    );
  }

  const readFile = deps.readFile ?? nodeReadFileSync;
  const statFile = deps.statFile ?? nodeStatSync;
  const packageDir = dirname(packageJsonPath);
  const manifestPath = join(packageDir, "runtime-manifest.json");
  const cliVersion = deps.cliVersion ?? getCliProductVersion();
  const manifest = validateRuntimeManifest(readJsonFile(readFile, manifestPath), {
    expectedTarget: target,
    expectedProductVersion: cliVersion,
  });

  const executablePath = resolve(packageDir, ...manifest.executable.split("/"));
  const confinement = relative(packageDir, executablePath);
  if (confinement === "" || confinement.startsWith("..") || isAbsolute(confinement)) {
    throw new Error(
      `Core executable escapes its package directory: ${manifest.executable}.`,
    );
  }

  let stat: { isFile(): boolean; mode: number };
  try {
    stat = statFile(executablePath);
  } catch {
    throw new Error(`Core executable is missing at ${executablePath}.`);
  }
  if (!stat.isFile()) {
    throw new Error(`Core executable is missing at ${executablePath}.`);
  }
  const hostPlatform = deps.platform ?? process.platform;
  if (!target.startsWith("win32") && hostPlatform !== "win32" && (stat.mode & 0o111) === 0) {
    throw new Error(`Core executable at ${executablePath} is not executable.`);
  }

  const hashBytes = deps.hashBytes ?? defaultHashBytes;
  const digest = hashBytes(readFile(executablePath)).toLowerCase();
  if (digest !== manifest.sha256) {
    throw new Error(
      `Core executable failed integrity check at ${executablePath}: sha256 mismatch. Reinstall ${packageName}.`,
    );
  }

  return { packageName, packageDir, executablePath, manifest };
}

function resolveOverride(override: string, deps: CoreExecutableDeps): ResolvedCore {
  if (!isAbsolute(override)) {
    throw new Error(
      `${CORE_EXECUTABLE_ENV} must be an absolute file path, got ${JSON.stringify(override)}.`,
    );
  }
  const statFile = deps.statFile ?? nodeStatSync;
  let stat: { isFile(): boolean; mode: number };
  try {
    stat = statFile(override);
  } catch {
    throw new Error(`${CORE_EXECUTABLE_ENV} points to a missing file: ${override}.`);
  }
  if (!stat.isFile()) {
    throw new Error(`${CORE_EXECUTABLE_ENV} points to a missing file: ${override}.`);
  }
  // Developer exception: the override is used as-is without manifest/hash checks.
  return {
    packageName: "(override)",
    packageDir: dirname(override),
    executablePath: override,
  };
}
