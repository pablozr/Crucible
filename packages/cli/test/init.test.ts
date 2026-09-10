import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { initializeProject } from "../src/services/project-initialization.js";

function repository(): string {
  const root = mkdtempSync(join(tmpdir(), "crucible-cli-"));
  execFileSync("git", ["init", "--quiet", root]);
  return root;
}

test("init creates stable project metadata without staging files", () => {
    const root = repository();
    const first = initializeProject(root);
    const second = initializeProject(root);

    assert.equal(first.id, second.id);
    assert.match(
        readFileSync(join(root, ".crucible", "project.json"), "utf8"),
        new RegExp(first.id),
    );
    assert.equal(
        execFileSync("git", ["-C", root, "status", "--porcelain"], {
            encoding: "utf8",
        }),
        "?? .crucible/\n",
    );
});


test("init rejects directories outside Git", () => {
    assert.throws(
        () => initializeProject(mkdtempSync(join(tmpdir(), "crucible-not-git-"))),
        /NOT_A_GIT_REPOSITORY/,
    );
});


test("init rejects invalid existing project metadata and configuration", () => {
    const root = repository();
    const projectDirectory = join(root, ".crucible");

    mkdirSync(projectDirectory);
    writeFileSync(join(projectDirectory, "project.json"), "{}\n");

    assert.throws(() => initializeProject(root), /INVALID_PROJECT_METADATA/);

    writeFileSync(
        join(projectDirectory, "project.json"),
        '{"project_id":"00000000-0000-7000-8000-000000000000"}\n',
    );
    writeFileSync(join(projectDirectory, "config.yaml"), "version: 2\n");

    assert.throws(() => initializeProject(root), /INVALID_PROJECT_CONFIG/);
});

type ContractCase = {
    id: string;
    projectFile: string;
    configFile: string | null;
    expected: string;
};

function contractRoot(): string {
    return resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "test", "fixtures", "project-contract");
}

test("init honors shared project-contract fixtures", () => {
    const root = contractRoot();
    const manifest = JSON.parse(readFileSync(join(root, "manifest.json"), "utf8")) as {
        version: number;
        cases: ContractCase[];
    };

    assert.equal(manifest.version, 1);
    assert.ok(manifest.cases.length > 0);

    for (const entry of manifest.cases) {
        const repo = repository();
        const projectDirectory = join(repo, ".crucible");
        mkdirSync(projectDirectory, { recursive: true });
        copyFileSync(join(root, entry.projectFile), join(projectDirectory, "project.json"));
        if (entry.configFile) {
            copyFileSync(join(root, entry.configFile), join(projectDirectory, "config.yaml"));
        }

        if (entry.expected === "ok") {
            const initialized = initializeProject(repo);
            const stored = JSON.parse(readFileSync(join(projectDirectory, "project.json"), "utf8")) as {
                project_id: string;
            };
            assert.equal(initialized.id, stored.project_id, entry.id);
            assert.ok(existsSync(join(projectDirectory, "config.yaml")), entry.id);
        } else {
            assert.throws(() => initializeProject(repo), new RegExp(entry.expected), entry.id);
        }
    }
});
