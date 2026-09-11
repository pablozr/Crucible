import type { SpawnOptions } from "node:child_process";

import {
  resolveCoreExecutable,
  type CoreExecutableDeps,
} from "../runtime/resolve.js";
import {
  defaultSpawnRunner,
  runForeground,
  type SpawnRunner,
} from "../shared/foreground-process.js";


export type ServeDeps = {
  runner?: SpawnRunner;
  /** Injectable executable resolution (tests). Defaults to the bundled runtime. */
  resolveExecutable?: () => string | Promise<string>;
  env?: NodeJS.ProcessEnv;
  /** Runtime resolution inputs (platform/libc/fs probes), mainly for tests. */
  runtime?: CoreExecutableDeps;
};


export async function runServe(deps: ServeDeps = {}): Promise<void> {
  const runner = deps.runner ?? defaultSpawnRunner;
  const executable = await (deps.resolveExecutable?.() ??
    resolveCoreExecutable({ ...deps.runtime, env: deps.env ?? deps.runtime?.env }));
  const options: SpawnOptions = {
    stdio: "inherit",
    shell: false,
    detached: false,
  };

  await runForeground(executable, [], options, runner);
}
