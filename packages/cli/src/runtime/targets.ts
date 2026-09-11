import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";

export const SUPPORTED_TARGETS = [
  "win32-x64",
  "darwin-x64",
  "darwin-arm64",
  "linux-x64-gnu",
] as const;

export type RuntimeTarget = (typeof SUPPORTED_TARGETS)[number];

export const TARGET_PACKAGE: Record<RuntimeTarget, string> = {
  "win32-x64": "@pablozrrrr/crucible-core-win32-x64",
  "darwin-x64": "@pablozrrrr/crucible-core-darwin-x64",
  "darwin-arm64": "@pablozrrrr/crucible-core-darwin-arm64",
  "linux-x64-gnu": "@pablozrrrr/crucible-core-linux-x64-gnu",
};

export type LinuxLibc = "glibc" | "musl" | "unknown";

export type TargetSelectionDeps = {
  platform?: NodeJS.Platform;
  arch?: NodeJS.Architecture;
  /** Explicit libc (tests/source override). When omitted on Linux, `probeLibc` runs. */
  libc?: LinuxLibc;
  /** Injectable libc probe for tests. Defaults to {@link defaultProbeLibc}. */
  probeLibc?: () => LinuxLibc;
};

export type SelectedRuntimeTarget = {
  target: RuntimeTarget;
  packageName: string;
};

function supportedList(): string {
  return SUPPORTED_TARGETS.join(", ");
}

function unsupportedPlatformMessage(platform: string, arch: string): string {
  return (
    `Unsupported platform ${platform}-${arch} for crucible-core. ` +
    `Supported targets: ${supportedList()}.`
  );
}

/**
 * Best-effort glibc-vs-musl probe without extra dependencies. Fail closed:
 * anything that is not positively identified as glibc reports "unknown",
 * and target selection refuses to run on "unknown"/"musl".
 */
export function defaultProbeLibc(): LinuxLibc {
  try {
    const report =
      typeof process.report?.getReport === "function"
        ? (process.report.getReport() as {
            header?: { glibcVersionRuntime?: unknown };
          })
        : undefined;
    const glibc = report?.header?.glibcVersionRuntime;
    if (typeof glibc === "string" && glibc.length > 0) {
      return "glibc";
    }
  } catch {
    // Fall through to file/ldd checks below.
  }

  try {
    if (existsSync("/etc/alpine-release")) {
      return "musl";
    }
    if (
      existsSync("/lib/ld-musl-x86_64.so.1") ||
      existsSync("/lib64/ld-musl-x86-64.so.1")
    ) {
      return "musl";
    }
  } catch {
    // Ignore and keep probing.
  }

  try {
    const output = execFileSync("ldd", ["--version"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    const text = String(output).toLowerCase();
    if (text.includes("musl")) {
      return "musl";
    }
    if (text.includes("glibc") || text.includes("gnu libc")) {
      return "glibc";
    }
  } catch {
    // ldd missing or failed: unknown.
  }

  return "unknown";
}

export function selectRuntimeTarget(deps: TargetSelectionDeps = {}): SelectedRuntimeTarget {
  const platform = deps.platform ?? process.platform;
  const arch = deps.arch ?? process.arch;

  if (platform === "win32" && arch === "x64") {
    return { target: "win32-x64", packageName: TARGET_PACKAGE["win32-x64"] };
  }

  if (platform === "darwin" && arch === "x64") {
    return { target: "darwin-x64", packageName: TARGET_PACKAGE["darwin-x64"] };
  }

  if (platform === "darwin" && arch === "arm64") {
    return { target: "darwin-arm64", packageName: TARGET_PACKAGE["darwin-arm64"] };
  }

  if (platform === "linux" && arch === "x64") {
    const libc = deps.libc ?? (deps.probeLibc ?? defaultProbeLibc)();
    if (libc !== "glibc") {
      const detail =
        libc === "musl"
          ? "detected musl"
          : "could not verify glibc (refusing to run to avoid a broken launch)";
      throw new Error(
        `Unsupported Linux libc (${detail}). crucible-core requires glibc (target linux-x64-gnu). ` +
          `Supported targets: ${supportedList()}.`,
      );
    }
    return { target: "linux-x64-gnu", packageName: TARGET_PACKAGE["linux-x64-gnu"] };
  }

  throw new Error(unsupportedPlatformMessage(platform, arch));
}
