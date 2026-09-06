#!/usr/bin/env node
import { initProject } from "./project-files.js";

const [command, directory] = process.argv.slice(2);

if (command !== "init" || process.argv.slice(2).length > 2) {
  console.error("Usage: crucible init [directory]");
  process.exitCode = 1;
} else {
  try {
    const project = initProject(directory ?? process.cwd());
    console.log(`Initialized Crucible project ${project.id} at ${project.root}`);
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}
