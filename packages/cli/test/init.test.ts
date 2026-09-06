import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { initProject } from "../src/project-files.js";

function repository(): string {
  const root = mkdtempSync(join(tmpdir(), "crucible-cli-"));
  execFileSync("git", ["init", "--quiet", root]);
  return root;
}

test("init creates stable project metadata without staging files", () => {
  const root = repository();
  const first = initProject(root);
  const second = initProject(root);
  assert.equal(first.id, second.id);
  assert.match(readFileSync(join(root, ".crucible", "project.json"), "utf8"), new RegExp(first.id));
  assert.equal(execFileSync("git", ["-C", root, "status", "--porcelain"], { encoding: "utf8" }), "?? .crucible/\n");
});

test("init rejects directories outside Git", () => {
  assert.throws(() => initProject(mkdtempSync(join(tmpdir(), "crucible-not-git-"))), /NOT_A_GIT_REPOSITORY/);
});

test("init rejects invalid existing project metadata and configuration", () => {
  const root = repository();
  const projectDirectory = join(root, ".crucible");
  mkdirSync(projectDirectory);
  writeFileSync(join(projectDirectory, "project.json"), "{}\n");
  assert.throws(() => initProject(root), /INVALID_PROJECT_METADATA/);

  writeFileSync(join(projectDirectory, "project.json"), '{"project_id":"00000000-0000-7000-8000-000000000000"}\n');
  writeFileSync(join(projectDirectory, "config.yaml"), "version: 2\n");
  assert.throws(() => initProject(root), /INVALID_PROJECT_CONFIG/);
});
