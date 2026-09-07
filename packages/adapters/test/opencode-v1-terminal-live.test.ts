import assert from "node:assert/strict";
import test from "node:test";

import { runOpenCodeV1TerminalProbe } from "./support/opencode-v1-terminal-probe.js";

const LIVE = process.env["CRUCIBLE_OPENCODE_TERMINAL_LIVE"] === "1";

test(
  "OC-V1-07 normal prompt return follows the final covered tool mutation",
  { skip: !LIVE },
  async () => {
    const observation = await runOpenCodeV1TerminalProbe({
      binary: process.env["CRUCIBLE_OPENCODE_BIN"] ?? "opencode",
    });

    assert.equal(observation.opencodeVersion, "1.18.28");
    assert.equal(observation.toolName, "write");
    assert.equal(observation.mainModelTurns, 2);
    assert.equal(observation.toolResultObservedByProvider, true);
    assert.equal(observation.mutationObservedBeforeFinalModelTurn, true);
    assert.equal(
      observation.sentinelContentAtPromptReturn,
      "oc-v1-07-mutation-complete\n",
    );
    assert.equal(observation.promptFinish, "stop");

    console.log(JSON.stringify({ probe: "OC-V1-07", ...observation }));
  },
);
