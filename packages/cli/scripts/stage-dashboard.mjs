// Stages built dashboard assets into packages/cli/dashboard.
// Source of truth: packages/dashboard/dist/dashboard/browser (produced by
// `pnpm --filter @pablozrrrr/dashboard build`). Runs explicitly after tsc via
// the CLI build script. Fails with an actionable error when assets are absent.
import { cpSync, existsSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const cliRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const source = join(cliRoot, "..", "dashboard", "dist", "dashboard", "browser");
const dest = join(cliRoot, "dashboard");
const marker = join(source, "index.html");

if (!existsSync(marker)) {
  console.error(
    `Dashboard assets are missing at ${source} (expected index.html). ` +
      `Run "pnpm --filter @pablozrrrr/dashboard build" first, then rebuild the CLI.`,
  );
  process.exit(1);
}

rmSync(dest, { recursive: true, force: true });
cpSync(source, dest, { recursive: true });
console.log(`Staged dashboard assets from ${source} to ${dest}.`);
