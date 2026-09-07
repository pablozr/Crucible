import assert from "node:assert/strict";
import test from "node:test";

import {
  FinalNextGatePrototype,
  type FrozenEvidence,
} from "./support/final-next-gate.prototype.js";

const FINAL_A: FrozenEvidence = {
  artifactId: "capture-a-final",
  treeHash: "sha256:a-final",
};

test("SOL-04 fences A before releasing timed-out input B", async () => {
  const gate = new FinalNextGatePrototype("tree-1", "task-a");
  const leaseA = gate.beginFinalCapture("task-a");
  let inputBResolved = false;
  const submitB = gate.submitNext("input-b");
  void submitB.then(() => {
    inputBResolved = true;
  });

  await Promise.resolve();

  assert.deepEqual(gate.snapshot().waitingInputs, ["input-b"]);
  assert.equal(inputBResolved, false);

  const releaseB = gate.expireSubmitBudget("input-b");
  const decisionB = await submitB;

  assert.deepEqual(decisionB, releaseB);
  assert.equal(decisionB.kind, "released_untracked");
  assert.equal(decisionB.dispatchAllowed, true);
  assert.equal(decisionB.baseline, null);

  const stalePublication = gate.publishFinal(leaseA, FINAL_A);
  const state = gate.snapshot();

  assert.equal(stalePublication.kind, "stale_discarded");
  assert.equal(state.task.status, "failed");
  assert.equal(state.task.failureCode, "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT");
  assert.equal(state.task.finalEvidence, null);
  assert.equal(state.inputs["input-b"]?.baseline, null);
  assert.deepEqual(state.trace, [
    "task-a:final_capture_started:g1",
    "input-b:waiting_for_final:g1",
    "task-a:final_capture_fenced:g2",
    "input-b:released_untracked:g2",
    "task-a:stale_final_discarded:g1/current-g2",
  ]);

  console.log(
    JSON.stringify({
      probe: "SOL-04",
      scenario: "next-input-budget-expires",
      stalePublication: stalePublication.kind,
      taskA: state.task,
      inputB: state.inputs["input-b"],
      trace: state.trace,
    }),
  );
});

test("SOL-04 reuses A's frozen final as B's baseline when A wins", async () => {
  const gate = new FinalNextGatePrototype("tree-1", "task-a");
  const leaseA = gate.beginFinalCapture("task-a");
  let inputBResolved = false;
  const submitB = gate.submitNext("input-b");
  void submitB.then(() => {
    inputBResolved = true;
  });

  await Promise.resolve();
  assert.equal(inputBResolved, false);

  const publicationA = gate.publishFinal(leaseA, FINAL_A);
  const decisionB = await submitB;
  const state = gate.snapshot();

  assert.equal(publicationA.kind, "published");
  assert.equal(state.task.status, "completed");
  assert.deepEqual(state.task.finalEvidence, FINAL_A);
  assert.equal(decisionB.kind, "tracked");
  assert.equal(decisionB.dispatchAllowed, true);
  assert.deepEqual(decisionB.baseline, FINAL_A);
  assert.deepEqual(state.inputs["input-b"]?.baseline, FINAL_A);
});
