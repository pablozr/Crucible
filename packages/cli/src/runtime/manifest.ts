import type { RuntimeTarget } from "./targets.js";

export const RUNTIME_MANIFEST_SCHEMA_VERSION = 1;
export const RUNTIME_MANIFEST_API_VERSION = "v1";
const SHA256_HEX = /^[0-9a-fA-F]{64}$/;

export type RuntimeManifest = {
  schemaVersion: 1;
  productVersion: string;
  apiVersion: "v1";
  target: RuntimeTarget;
  /** POSIX-style path relative to the optional package dir. */
  executable: string;
  /** Lowercase hex sha256 of the executable file. */
  sha256: string;
};

export type ValidateManifestOptions = {
  expectedTarget: RuntimeTarget;
  expectedProductVersion: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Validate a parsed runtime-manifest.json against the build contract.
 * Rejects schema/API/target/product mismatches, executable traversal,
 * and malformed hashes so a forged or stale package cannot be launched.
 */
export function validateRuntimeManifest(
  raw: unknown,
  options: ValidateManifestOptions,
): RuntimeManifest {
  if (!isRecord(raw)) {
    throw new Error("Invalid runtime manifest: expected a JSON object.");
  }

  if (raw["schemaVersion"] !== RUNTIME_MANIFEST_SCHEMA_VERSION) {
    throw new Error(
      `Unsupported runtime manifest schemaVersion ${String(raw["schemaVersion"])} (expected ${RUNTIME_MANIFEST_SCHEMA_VERSION}).`,
    );
  }

  const productVersion = raw["productVersion"];
  if (typeof productVersion !== "string" || productVersion.length === 0) {
    throw new Error("Invalid runtime manifest: productVersion must be a non-empty string.");
  }
  if (productVersion !== options.expectedProductVersion) {
    throw new Error(
      `Runtime manifest productVersion mismatch: manifest has ${productVersion}, CLI expects ${options.expectedProductVersion}. Reinstall matching versions.`,
    );
  }

  if (raw["apiVersion"] !== RUNTIME_MANIFEST_API_VERSION) {
    throw new Error(
      `Unsupported runtime manifest apiVersion ${String(raw["apiVersion"])} (expected ${RUNTIME_MANIFEST_API_VERSION}).`,
    );
  }

  if (raw["target"] !== options.expectedTarget) {
    throw new Error(
      `Runtime manifest target mismatch: manifest has ${String(raw["target"])}, expected ${options.expectedTarget}.`,
    );
  }

  const executable = raw["executable"];
  if (typeof executable !== "string" || executable.length === 0) {
    throw new Error("Invalid runtime manifest: executable must be a non-empty string.");
  }
  if (executable.includes("\\")) {
    throw new Error(
      `Invalid runtime manifest executable ${JSON.stringify(executable)}: must use POSIX "/" separators.`,
    );
  }
  if (executable.startsWith("/") || /^[A-Za-z]:/.test(executable)) {
    throw new Error(
      `Invalid runtime manifest executable ${JSON.stringify(executable)}: must be relative to the package directory.`,
    );
  }
  const segments = executable.split("/");
  for (const segment of segments) {
    if (segment.length === 0 || segment === "." || segment === "..") {
      throw new Error(
        `Invalid runtime manifest executable ${JSON.stringify(executable)}: must be a relative path without traversal.`,
      );
    }
  }

  const sha256 = raw["sha256"];
  if (typeof sha256 !== "string" || !SHA256_HEX.test(sha256)) {
    throw new Error("Invalid runtime manifest: sha256 must be 64 hex characters.");
  }

  return {
    schemaVersion: 1,
    productVersion,
    apiVersion: "v1",
    target: raw["target"] as RuntimeTarget,
    executable,
    sha256: sha256.toLowerCase(),
  };
}
