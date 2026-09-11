import { createRequire } from "node:module";

/** Build-time product version fallback; the file value below stays in sync. */
export const CLI_PRODUCT_VERSION_FALLBACK = "0.1.0";

/**
 * Read the CLI's own product version from packages/cli/package.json.
 * Resolved relative to this module (works from both src/ and dist/),
 * so the version is never duplicated across sources.
 */
export function getCliProductVersion(fromUrl: string = import.meta.url): string {
  try {
    const require = createRequire(fromUrl);
    const pkg = require("../../package.json") as { version?: unknown };
    if (typeof pkg.version === "string" && pkg.version.length > 0) {
      return pkg.version;
    }
  } catch {
    // Fall through to the fallback below.
  }
  return CLI_PRODUCT_VERSION_FALLBACK;
}
