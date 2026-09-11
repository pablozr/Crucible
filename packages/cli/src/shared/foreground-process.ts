import { spawn, type SpawnOptions } from "node:child_process";


export type ForegroundExit = {
  code: number | null;
  signal: NodeJS.Signals | null;
};


export type SpawnRunner = (
  command: string,
  args: string[],
  options: SpawnOptions,
) => Promise<ForegroundExit>;


function isErrno(error: unknown): error is NodeJS.ErrnoException {
  return (
    !!error &&
    typeof error === "object" &&
    "code" in error &&
    typeof (error as { code?: unknown }).code === "string"
  );
}


export const defaultSpawnRunner: SpawnRunner = (command, args, options) =>
  new Promise<ForegroundExit>((resolve, reject) => {
    const child = spawn(command, args, options);
    child.on("error", reject);
    child.on("exit", (code, signal) => resolve({ code, signal }));
  });


export async function runForeground(
  command: string,
  args: string[],
  options: SpawnOptions,
  runner: SpawnRunner = defaultSpawnRunner,
): Promise<void> {
  let exit: ForegroundExit;

  try {
    exit = await runner(command, args, options);
  } catch (error) {
    if (isErrno(error) && error.code === "ENOENT") {
      throw new Error(
        `Cannot start ${command}: executable not found in PATH. Install the core and ensure ${command} is on PATH, then retry.`,
      );
    }

    throw new Error(
      `Cannot start ${command}: ${error instanceof Error ? error.message : String(error)}`,
    );
  }

  if (exit.signal) {
    throw new Error(`${command} terminated with signal ${exit.signal}.`);
  }

  if ((exit.code ?? 1) !== 0) {
    throw new Error(`${command} exited with code ${exit.code}.`);
  }
}
