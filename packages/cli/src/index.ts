#!/usr/bin/env node
import { runInit } from "./commands/init.js";
import { runDashboard } from "./commands/dashboard.js";
import { runServe } from "./commands/serve.js";
import { runStatus } from "./commands/status.js";
import { getCliProductVersion } from "./runtime/version.js";
import { realpathSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { SpawnRunner } from "./shared/foreground-process.js";
import type { DashboardServerFactory, OpenBrowserFn } from "./commands/dashboard.js";


export const USAGE = `Usage: crucible <command> [options]

Commands:
  init [directory]   Initialize a Crucible project
  serve              Start crucible-core in the foreground
  dashboard          Start the dashboard server
  status [--json]    Show core status
  --version          Print the CLI version`;


export type CliDeps = {
  initFn?: (directory: string) => void;
  serveRunner?: SpawnRunner;
  dashboardAssetsDir?: string;
  dashboardDir?: string;
  dashboardServerFactory?: DashboardServerFactory;
  dashboardOpenBrowser?: OpenBrowserFn;
  fetchFn?: typeof fetch;
  stdout?: (message: string) => void;
  stderr?: (message: string) => void;
};


export async function runCli(argv: string[], deps: CliDeps = {}): Promise<number> {
  const stdout = deps.stdout ?? console.log;
  const stderr = deps.stderr ?? ((message: string) => console.error(message));

  const [command, ...rest] = argv;

  if (command === "--version") {
    if (rest.length > 0) {
      stderr(USAGE);
      return 1;
    }

    stdout(getCliProductVersion());
    return 0;
  }

  if (command === "init") {
    if (rest.length > 1) {
      stderr(USAGE);
      return 1;
    }

    try {
      (deps.initFn ?? runInit)(rest[0] ?? process.cwd());
    } catch (error) {
      stderr(error instanceof Error ? error.message : String(error));
      return 1;
    }

    return 0;
  }

  if (command === "serve") {
    if (rest.length > 0) {
      stderr(USAGE);
      return 1;
    }

    try {
      await runServe({ runner: deps.serveRunner });
    } catch (error) {
      stderr(error instanceof Error ? error.message : String(error));
      return 1;
    }

    return 0;
  }

  if (command === "dashboard") {
    if (rest.length > 0) {
      stderr(USAGE);
      return 1;
    }

    try {
      await runDashboard({
        assetsDir: deps.dashboardAssetsDir ?? deps.dashboardDir,
        serverFactory: deps.dashboardServerFactory,
        openBrowser: deps.dashboardOpenBrowser,
      });
    } catch (error) {
      stderr(error instanceof Error ? error.message : String(error));
      return 1;
    }

    return 0;
  }

  if (command === "status") {
    if (rest.length > 1 || (rest.length === 1 && rest[0] !== "--json")) {
      stderr(USAGE);
      return 1;
    }

    try {
      await runStatus(
        { json: rest[0] === "--json" },
        { fetchFn: deps.fetchFn, stdout },
      );
    } catch (error) {
      stderr(error instanceof Error ? error.message : String(error));
      return 1;
    }

    return 0;
  }

  stderr(USAGE);
  return 1;
}


export function isMainModule(argv1: string | undefined, metaUrl: string): boolean {
  if (!argv1) {
    return false;
  }

  try {
    const entryReal = realpathSync(resolve(argv1));
    const selfReal = realpathSync(fileURLToPath(metaUrl));

    if (process.platform === "win32") {
      return entryReal.toLowerCase() === selfReal.toLowerCase();
    }

    return entryReal === selfReal;
  } catch {
    return false;
  }
}


if (isMainModule(process.argv[1], import.meta.url)) {
  const code = await runCli(process.argv.slice(2));
  process.exitCode = code;
}
