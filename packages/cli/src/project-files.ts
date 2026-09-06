import { execFileSync } from "node:child_process";
import { existsSync, lstatSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { parseDocument } from "yaml";

const defaultConfig = "version: 1\ntracking:\n  max_snapshot_file_size_bytes: 1048576\n";
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export type InitializedProject = { id: string; root: string };

function gitRoot(directory: string): string {
  try {
    return execFileSync("git", ["-C", resolve(directory), "rev-parse", "--show-toplevel"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    throw new Error("NOT_A_GIT_REPOSITORY: crucible init must run inside a Git repository.");
  }
}

function existingProjectId(path: string): string | undefined {
  if (!existsSync(path)) return undefined;
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, "utf8"));
  } catch {
    throw new Error("INVALID_PROJECT_METADATA: project.json must contain valid JSON.");
  }
  if (
    !parsed ||
    typeof parsed !== "object" ||
    Object.keys(parsed).length !== 1 ||
    typeof (parsed as { project_id?: unknown }).project_id !== "string" ||
    !uuidPattern.test((parsed as { project_id: string }).project_id)
  ) {
    throw new Error("INVALID_PROJECT_METADATA: project.json must contain only a valid project_id UUID.");
  }
  return (parsed as { project_id: string }).project_id;
}

function validateConfig(path: string): void {
  if (!existsSync(path)) return;
  const document = parseDocument(readFileSync(path, "utf8"), { uniqueKeys: true });
  const config = document.toJS();
  if (document.errors.length || !config || typeof config !== "object" || Array.isArray(config)) {
    throw new Error("INVALID_PROJECT_CONFIG: config.yaml must be valid YAML.");
  }
  const tracking = (config as { tracking?: unknown }).tracking ?? {};
  if (typeof tracking !== "object" || Array.isArray(tracking)) {
    throw new Error("INVALID_PROJECT_CONFIG: tracking must be a mapping.");
  }
  const limit = (tracking as { max_snapshot_file_size_bytes?: unknown }).max_snapshot_file_size_bytes;
  if ((config as { version?: unknown }).version !== 1 || (limit !== undefined && (!Number.isInteger(limit) || (limit as number) <= 0))) {
    throw new Error("INVALID_PROJECT_CONFIG: max_snapshot_file_size_bytes must be a positive integer.");
  }
}

export function initProject(directory: string): InitializedProject {
  const root = gitRoot(directory);
  const crucibleDir = resolve(root, ".crucible");
  const projectPath = resolve(crucibleDir, "project.json");
  const configPath = resolve(crucibleDir, "config.yaml");
  if (existsSync(crucibleDir) && lstatSync(crucibleDir).isSymbolicLink()) {
    throw new Error("INVALID_PROJECT_METADATA: .crucible must not be a symbolic link.");
  }
  mkdirSync(crucibleDir, { recursive: true });

  const id = existingProjectId(projectPath) ?? randomUUID();
  validateConfig(configPath);
  if (!existsSync(projectPath)) {
    writeFileSync(projectPath, `${JSON.stringify({ project_id: id }, null, 2)}\n`, { encoding: "utf8", flag: "wx" });
  }
  if (!existsSync(configPath)) {
    writeFileSync(configPath, defaultConfig, { encoding: "utf8", flag: "wx" });
  }
  return { id, root };
}
