import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { resolveProject } from "../src/runtime/project-resolver.js";

const PROJECT_ID = "123e4567-e89b-42d3-a456-426614174000";

function normalizePath(path: string): string {
  return path.replace(/\\/g, "/");
}

function gitRepository(): string {
  const root = mkdtempSync(join(tmpdir(), "crucible-adapter-"));
  execFileSync("git", ["init", "--quiet", root]);
  return root;
}

function writeProject(root: string, body: string): string {
  const directory = join(root, ".crucible");
  mkdirSync(directory, { recursive: true });
  const path = join(directory, "project.json");
  writeFileSync(path, body);
  return path;
}

test("resolves project id and git root without writes", () => {
  const root = gitRepository();
  const path = writeProject(root, `${JSON.stringify({ project_id: PROJECT_ID }, null, 2)}\n`);
  const beforeContent = readFileSync(path, "utf8");
  const beforeStat = statSync(path);
  const beforeEntries = readdirSync(join(root, ".crucible")).sort();

  const resolved = resolveProject(root);

  assert.equal(resolved.projectId, PROJECT_ID);
  assert.equal(normalizePath(resolved.gitRoot), normalizePath(root));
  assert.equal(readFileSync(path, "utf8"), beforeContent);
  assert.equal(statSync(path).mtimeMs, beforeStat.mtimeMs);
  assert.deepEqual(readdirSync(join(root, ".crucible")).sort(), beforeEntries);
});

test("resolves from a nested workspace directory", () => {
  const root = gitRepository();
  writeProject(root, JSON.stringify({ project_id: PROJECT_ID }));
  mkdirSync(join(root, "nested", "deep"), { recursive: true });

  const resolved = resolveProject(join(root, "nested", "deep"));

  assert.equal(resolved.projectId, PROJECT_ID);
  assert.equal(normalizePath(resolved.gitRoot), normalizePath(root));
});

test("relative workspace path is rejected", () => {
  assert.throws(() => resolveProject("relative/path"), /PATH_MUST_BE_ABSOLUTE/);
});

test("directory outside git is rejected", () => {
  const root = mkdtempSync(join(tmpdir(), "crucible-adapter-nogit-"));
  assert.throws(() => resolveProject(root), /NOT_A_GIT_REPOSITORY/);
});

test("workspace without project metadata is rejected", () => {
  const root = gitRepository();
  assert.throws(() => resolveProject(root), /UNINITIALIZED_PROJECT/);
  assert.equal(readdirSync(root).sort().includes(".crucible"), false);
});

test("invalid project metadata is rejected", () => {
  const root = gitRepository();
  writeProject(root, "{ not json");
  assert.throws(() => resolveProject(root), /INVALID_PROJECT_METADATA/);

  writeProject(root, JSON.stringify({ project_id: "not-a-uuid" }));
  assert.throws(() => resolveProject(root), /INVALID_PROJECT_METADATA/);

  writeProject(
    root,
    JSON.stringify({ project_id: PROJECT_ID, extra: "nope" }),
  );
  assert.throws(() => resolveProject(root), /INVALID_PROJECT_METADATA/);
});
