/**
 * Throwaway SOL-04 protocol prototype.
 *
 * Question: can a worktree generation fence prevent a delayed final capture
 * from publishing after the next controlled input is released?
 *
 * Delete this module when the protocol moves into the Core/adapter boundary.
 * It deliberately has no persistence, transport, or production integration.
 */

export interface FrozenEvidence {
  readonly artifactId: string;
  readonly treeHash: string;
}

export interface FinalCaptureLease {
  readonly treeId: string;
  readonly taskId: string;
  readonly generation: number;
}

export interface SubmitDecision {
  readonly inputId: string;
  readonly kind: "tracked" | "released_untracked";
  readonly dispatchAllowed: true;
  readonly baseline: FrozenEvidence | null;
  readonly generation: number;
}

export type FinalPublication =
  | {
      readonly kind: "published";
      readonly evidence: FrozenEvidence;
      readonly generation: number;
    }
  | {
      readonly kind: "stale_discarded";
      readonly generation: number;
      readonly currentGeneration: number;
    };

interface WaitingInput {
  baseline: FrozenEvidence | null;
  decision: SubmitDecision | null;
  resolve: (decision: SubmitDecision) => void;
  state: "waiting" | "admitted" | "released_untracked";
}

interface TaskState {
  failureCode: "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT" | null;
  finalEvidence: FrozenEvidence | null;
  id: string;
  status: "running" | "finalizing" | "completed" | "failed";
}

export interface FinalNextGateSnapshot {
  readonly generation: number;
  readonly inputs: Readonly<
    Record<
      string,
      {
        readonly baseline: FrozenEvidence | null;
        readonly state: WaitingInput["state"];
      }
    >
  >;
  readonly task: Readonly<TaskState>;
  readonly trace: readonly string[];
  readonly treeId: string;
  readonly waitingInputs: readonly string[];
}

export class FinalNextGatePrototype {
  readonly #inputs = new Map<string, WaitingInput>();
  readonly #task: TaskState;
  readonly #trace: string[] = [];
  readonly #treeId: string;
  #generation = 0;

  constructor(treeId: string, taskId: string) {
    this.#treeId = treeId;
    this.#task = {
      failureCode: null,
      finalEvidence: null,
      id: taskId,
      status: "running",
    };
  }

  beginFinalCapture(taskId: string): FinalCaptureLease {
    if (taskId !== this.#task.id || this.#task.status !== "running") {
      throw new Error(`Task ${taskId} cannot begin final capture`);
    }

    this.#generation += 1;
    this.#task.status = "finalizing";
    this.#trace.push(
      `${taskId}:final_capture_started:g${this.#generation}`,
    );

    return Object.freeze({
      generation: this.#generation,
      taskId,
      treeId: this.#treeId,
    });
  }

  submitNext(inputId: string): Promise<SubmitDecision> {
    if (this.#task.status !== "finalizing") {
      throw new Error(`Input ${inputId} did not encounter a finalizing task`);
    }
    if (this.#inputs.has(inputId)) {
      throw new Error(`Input ${inputId} was already submitted`);
    }

    let resolveDecision!: (decision: SubmitDecision) => void;
    const pending = new Promise<SubmitDecision>((resolve) => {
      resolveDecision = resolve;
    });

    this.#inputs.set(inputId, {
      baseline: null,
      decision: null,
      resolve: resolveDecision,
      state: "waiting",
    });
    this.#trace.push(
      `${inputId}:waiting_for_final:g${this.#generation}`,
    );

    return pending;
  }

  expireSubmitBudget(inputId: string): SubmitDecision {
    const input = this.#requireWaitingInput(inputId);
    if (this.#task.status !== "finalizing") {
      throw new Error(`Task ${this.#task.id} is not finalizing`);
    }

    // This transition represents one durable Core commit. The generation is
    // fenced before the adapter receives permission to dispatch the input.
    this.#generation += 1;
    this.#task.status = "failed";
    this.#task.failureCode = "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT";
    this.#task.finalEvidence = null;
    this.#trace.push(
      `${this.#task.id}:final_capture_fenced:g${this.#generation}`,
    );

    const decision: SubmitDecision = Object.freeze({
      baseline: null,
      dispatchAllowed: true,
      generation: this.#generation,
      inputId,
      kind: "released_untracked",
    });
    input.decision = decision;
    input.state = "released_untracked";
    this.#trace.push(
      `${inputId}:released_untracked:g${this.#generation}`,
    );
    input.resolve(decision);

    return decision;
  }

  publishFinal(
    lease: FinalCaptureLease,
    evidence: FrozenEvidence,
  ): FinalPublication {
    if (
      lease.treeId !== this.#treeId ||
      lease.taskId !== this.#task.id ||
      lease.generation !== this.#generation ||
      this.#task.status !== "finalizing"
    ) {
      this.#trace.push(
        `${lease.taskId}:stale_final_discarded:g${lease.generation}` +
          `/current-g${this.#generation}`,
      );
      return Object.freeze({
        currentGeneration: this.#generation,
        generation: lease.generation,
        kind: "stale_discarded",
      });
    }

    const frozenEvidence = Object.freeze({ ...evidence });
    this.#task.finalEvidence = frozenEvidence;
    this.#task.status = "completed";
    this.#trace.push(
      `${lease.taskId}:final_published:g${this.#generation}`,
    );

    for (const [inputId, input] of this.#inputs) {
      if (input.state !== "waiting") {
        continue;
      }
      const decision: SubmitDecision = Object.freeze({
        baseline: frozenEvidence,
        dispatchAllowed: true,
        generation: this.#generation,
        inputId,
        kind: "tracked",
      });
      input.baseline = frozenEvidence;
      input.decision = decision;
      input.state = "admitted";
      this.#trace.push(
        `${inputId}:admitted_with_frozen_baseline:g${this.#generation}`,
      );
      input.resolve(decision);
    }

    return Object.freeze({
      evidence: frozenEvidence,
      generation: this.#generation,
      kind: "published",
    });
  }

  snapshot(): FinalNextGateSnapshot {
    const inputs = Object.fromEntries(
      [...this.#inputs].map(([inputId, input]) => [
        inputId,
        {
          baseline: input.baseline
            ? Object.freeze({ ...input.baseline })
            : null,
          state: input.state,
        },
      ]),
    );

    return Object.freeze({
      generation: this.#generation,
      inputs: Object.freeze(inputs),
      task: Object.freeze({
        ...this.#task,
        finalEvidence: this.#task.finalEvidence
          ? Object.freeze({ ...this.#task.finalEvidence })
          : null,
      }),
      trace: Object.freeze([...this.#trace]),
      treeId: this.#treeId,
      waitingInputs: Object.freeze(
        [...this.#inputs]
          .filter(([, input]) => input.state === "waiting")
          .map(([inputId]) => inputId),
      ),
    });
  }

  #requireWaitingInput(inputId: string): WaitingInput {
    const input = this.#inputs.get(inputId);
    if (!input || input.state !== "waiting") {
      throw new Error(`Input ${inputId} is not waiting`);
    }
    return input;
  }
}
