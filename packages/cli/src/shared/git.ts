import { execFileSync } from "node:child_process";
import { resolve } from "node:path";


export function gitRoot(directory: string): string {
  try {
    return execFileSync(
      "git",
      ["-C", resolve(directory), "rev-parse", "--show-toplevel"],
      {
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
      },
    ).trim();
  } catch {
    throw new Error(
      "NOT_A_GIT_REPOSITORY: crucible init must run inside a Git repository.",
    );
  }
}
