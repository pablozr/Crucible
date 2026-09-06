#!/usr/bin/env node
import { runInit } from "./commands/init.js";

const [command, directory] = process.argv.slice(2);


if (command !== "init" || process.argv.slice(2).length > 2) {
    console.error("Usage: crucible init [directory]");
    process.exitCode = 1;
} else {
    try {
        runInit(directory ?? process.cwd());
    } catch (error) {
        console.error(error instanceof Error ? error.message : String(error));
        process.exitCode = 1;
  }
}
