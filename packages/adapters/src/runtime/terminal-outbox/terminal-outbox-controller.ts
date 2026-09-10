import { join, resolve } from "node:path";

import type {
  Admission,
  AdmittedTerminalInput,
  CandidateReservationInput,
  OutboxDelivery,
  OutboxDeliveryOptions,
  TerminalAbortReason,
  TerminalEnvelopeInput,
  TerminalOutboxPolicy,
} from "../contracts.js";
import { resolveCrucibleDataDir } from "./terminal-outbox-store.js";
import { TerminalOutbox } from "../terminal-outbox.js";

// Lifecycle over the durable outbox: start/stop, timers and in-flight
// promises. It drives only the facade's public API (deliver/deliverDue plus
// the store delegates); persistence lives in terminal-outbox-store.ts and
// transport in terminal-delivery.ts. This controller never touches SQL or
// HTTP directly.
export class TerminalOutboxController {
  readonly outbox: TerminalOutbox;
  private running = false;
  private closed = false;
  private timerHandle: unknown;
  private drainPromise: Promise<void> | undefined;
  private readonly deliveries = new Set<Promise<unknown>>();

  private constructor(
    outbox: TerminalOutbox,
    private readonly policy: TerminalOutboxPolicy,
    private readonly deliveryOptions: Omit<OutboxDeliveryOptions, "policy">,
  ) {
    this.outbox = outbox;
  }

  get reservationBytes(): number {
    return this.outbox.reservationBytes;
  }

  static async open(
    policy: TerminalOutboxPolicy,
    deliveryOptions: Omit<OutboxDeliveryOptions, "policy">,
  ): Promise<TerminalOutboxController> {
    return new TerminalOutboxController(await TerminalOutbox.open(policy), policy, deliveryOptions);
  }

  start(): void {
    if (this.closed) throw new Error("TERMINAL_OUTBOX_CONTROLLER_CLOSED");
    if (this.running) return;
    this.running = true;
    this.requestDrain();
  }

  async stop(): Promise<void> {
    if (this.closed) return;
    this.running = false;
    if (this.timerHandle !== undefined) {
      this.policy.timer.cancel(this.timerHandle);
      this.timerHandle = undefined;
    }
    await this.drainPromise;
    await Promise.all(this.deliveries);
    this.outbox.close();
    this.closed = true;
  }

  reserve(agentSessionId: string, executionId: string, profile: string): string | null {
    return this.outbox.reserve(agentSessionId, executionId, profile);
  }

  reserveCandidate(input: CandidateReservationInput): string | null {
    const id = this.outbox.reserveCandidate(input);
    this.scheduleNextWake();
    return id;
  }

  cancelReservation(id: string): void {
    this.outbox.cancelReservation(id);
  }

  bindAdmission(id: string, taskId: string, inputId: string): void {
    this.outbox.bindAdmission(id, taskId, inputId);
  }

  reconcileCandidateResponse(id: string, admission: Admission): void {
    this.outbox.reconcileCandidateResponse(id, admission);
    this.scheduleNextWake();
  }

  markAdmissionAborted(agentSessionId: string, executionId: string, reason: TerminalAbortReason): string {
    const id = this.outbox.markAdmissionAborted(agentSessionId, executionId, reason);
    this.scheduleNextWake();
    return id;
  }

  storeCompletionForAdmission(input: AdmittedTerminalInput): {
    obligationId: string;
    eventId: string;
    bytes: number;
    payloadHash: string;
  } {
    const stored = this.outbox.storeCompletionForAdmission(input);
    this.scheduleNextWake();
    return stored;
  }

  storeCompletion(id: string, input: TerminalEnvelopeInput): { eventId: string; bytes: number; payloadHash: string } {
    const stored = this.outbox.storeCompletion(id, input);
    this.scheduleNextWake();
    return stored;
  }

  async deliver(id?: string): Promise<OutboxDelivery | undefined> {
    await this.drainPromise;
    const pending = this.outbox.deliver(this.deliveryOptions, id);
    this.deliveries.add(pending);
    try {
      return await pending;
    } finally {
      this.deliveries.delete(pending);
      this.scheduleNextWake();
    }
  }

  private requestDrain(): void {
    if (!this.running || this.drainPromise) return;
    if (this.timerHandle !== undefined) {
      this.policy.timer.cancel(this.timerHandle);
      this.timerHandle = undefined;
    }
    this.drainPromise = this.outbox.deliverDue(this.deliveryOptions)
      .catch(() => {
        // A later persisted wake-up retries recoverable delivery failures.
      })
      .finally(() => {
        this.drainPromise = undefined;
        this.scheduleNextWake();
      });
  }

  private scheduleNextWake(): void {
    if (!this.running || this.drainPromise) return;
    if (this.timerHandle !== undefined) {
      this.policy.timer.cancel(this.timerHandle);
      this.timerHandle = undefined;
    }
    const wakeAt = this.outbox.nextWakeAt();
    if (wakeAt === undefined) return;
    this.timerHandle = this.policy.timer.schedule(() => {
      this.timerHandle = undefined;
      this.requestDrain();
    }, Math.max(0, wakeAt - this.policy.clock()));
  }
}

const controllers = new Map<string, TerminalOutboxController>();
const pendingControllers = new Map<string, Promise<TerminalOutboxController>>();

function controllerKey(policy: TerminalOutboxPolicy): string {
  return join(resolve(policy.dataDir ?? resolveCrucibleDataDir()), "adapter-terminal-outbox.db");
}

export async function startTerminalOutboxController(
  policy: TerminalOutboxPolicy,
  deliveryOptions: Omit<OutboxDeliveryOptions, "policy">,
): Promise<TerminalOutboxController> {
  const key = controllerKey(policy);
  // Concurrent dispatches share one creation promise so a lost race cannot
  // leak a second singleton connection behind the map entry.
  let pending = pendingControllers.get(key);
  if (!pending) {
    pending = (async () => {
      let controller = controllers.get(key);
      if (!controller) {
        controller = await TerminalOutboxController.open(policy, deliveryOptions);
        controllers.set(key, controller);
      }
      return controller;
    })();
    pendingControllers.set(key, pending);
    pending.finally(() => {
      if (pendingControllers.get(key) === pending) pendingControllers.delete(key);
    }).catch(() => undefined);
  }
  const controller = await pending;
  controller.start();
  return controller;
}

export async function stopTerminalOutboxController(policy: TerminalOutboxPolicy): Promise<void> {
  const key = controllerKey(policy);
  const controller = controllers.get(key);
  if (!controller) return;
  controllers.delete(key);
  await controller.stop();
}
