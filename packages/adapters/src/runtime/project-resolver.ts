import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";

export type ResolvedProject = {
  projectId: string;
  gitRoot: string;
};

export type ResolveProjectFn = (
  workspacePath: string,
) => ResolvedProject | Promise<ResolvedProject>;

const uuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function gitRoot(directory: string): string {
  try {
    return execFileSync(
      "git",
      ["-C", resolve(directory), "rev-parse", "--show-toplevel"],
      { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] },
    ).trim();
  } catch {
    throw new Error(
      "NOT_A_GIT_REPOSITORY: workspace is not inside a Git repository.",
    );
  }
}

export function resolveProject(workspacePath: string): ResolvedProject {
  if (!isAbsolute(workspacePath)) {
    throw new Error("PATH_MUST_BE_ABSOLUTE: workspacePath must be absolute.");
  }
  const root = gitRoot(workspacePath);
  const projectPath = resolve(root, ".crucible", "project.json");
  if (!existsSync(projectPath)) {
    throw new Error(
      "UNINITIALIZED_PROJECT: .crucible/project.json not found.",
    );
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(projectPath, "utf8"));
  } catch {
    throw new Error(
      "INVALID_PROJECT_METADATA: project.json must contain valid JSON.",
    );
  }
  if (
    !parsed ||
    typeof parsed !== "object" ||
    Object.keys(parsed).length !== 1 ||
    typeof (parsed as { project_id?: unknown }).project_id !== "string" ||
    !uuidPattern.test((parsed as { project_id: string }).project_id)
  ) {
    throw new Error(
      "INVALID_PROJECT_METADATA: project.json must contain only a valid project_id UUID.",
    );
  }
  return { projectId: (parsed as { project_id: string }).project_id, gitRoot: root };
}
