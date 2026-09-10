import type {
  AdmittedTerminalInput,
  Admission,
  CandidateReservationInput,
  OutboxDelivery,
  OutboxDeliveryOptions,
  TerminalAbortReason,
  TerminalEnvelopeInput,
  TerminalOutboxPolicy,
  TerminalOutboxTimer,
} from "./contracts.js";
import { deliverTerminal, deliverTerminalDue } from "./terminal-outbox/terminal-delivery.js";
import {
  OUTBOX_MAX_BYTES,
  TERMINAL_MAX_AUTHORIZATION_WINDOW_MS,
  TERMINAL_RESERVATION_BYTES,
  TerminalOutboxStore,
} from "./terminal-outbox/terminal-outbox-store.js";

export type {
  AdmittedTerminalInput,
  CandidateReservationInput,
  OutboxDelivery,
  OutboxDeliveryOptions,
  TerminalAbortReason,
  TerminalEnvelopeInput,
  TerminalOutboxPolicy,
  TerminalOutboxTimer,
} from "./contracts.js";

export {
  OUTBOX_MAX_BYTES,
  resolveCrucibleDataDir,
  TERMINAL_ABORT_PERSISTED,
  TERMINAL_MAX_AUTHORIZATION_WINDOW_MS,
  TERMINAL_RESERVATION_BYTES,
} from "./terminal-outbox/terminal-outbox-store.js";

export {
  startTerminalOutboxController,
  stopTerminalOutboxController,
  TerminalOutboxController,
} from "./terminal-outbox/terminal-outbox-controller.js";

const defaultTimer: TerminalOutboxTimer = {
  schedule: (callback, delayMs) => setTimeout(callback, delayMs),
  cancel: (handle) => clearTimeout(handle as NodeJS.Timeout),
};

// Fills production defaults around an explicit or injected test policy;
// terminal behavior itself stays opt-in at the dispatch layer. The default
// lifecycle timer lives here (composition), never in the store, so the
// persistence layer stays free of setTimeout/clearTimeout ownership.
export function createTerminalOutboxPolicy(
  overrides: Partial<TerminalOutboxPolicy> = {},
): TerminalOutboxPolicy {
  return {
    maxBytes: OUTBOX_MAX_BYTES,
    reservationBytes: TERMINAL_RESERVATION_BYTES,
    authorizationWindowMs: TERMINAL_MAX_AUTHORIZATION_WINDOW_MS,
    leaseMs: 5_000,
    backoffBaseMs: 500,
    backoffMaxMs: 30_000,
    clock: () => Date.now(),
    jitter: (maximumDelayMs) => Math.random() * maximumDelayMs,
    timer: defaultTimer,
    busyTimeoutMs: 5_000,
    ...overrides,
  };
}

// Public facade (R7): persistence lives in terminal-outbox/terminal-outbox-store.ts,
// transport in terminal-outbox/terminal-delivery.ts and the lifecycle in
// terminal-outbox/terminal-outbox-controller.ts. The store is held in an
// ECMAScript-private field so the delivery internals (lease/finish/retry,
// candidate release/retry, due listings) never appear on this public surface:
// the declaration only carries `#private`, never the store type. Transport
// receives the store from inside these methods, never through public API.
export class TerminalOutbox {
  #store!: TerminalOutboxStore;

  private constructor() {}

  static async open(policy: TerminalOutboxPolicy): Promise<TerminalOutbox> {
    const outbox = new TerminalOutbox();
    outbox.#store = await TerminalOutboxStore.open(policy);
    return outbox;
  }

  get databasePath(): string {
    return this.#store.databasePath;
  }

  get driverName(): string {
    return this.#store.driverName;
  }

  get reservationBytes(): number {
    return this.#store.reservationBytes;
  }

  close(): void {
    this.#store.close();
  }

  reserve(agentSessionId: string, executionId: string, profile: string): string | null {
    return this.#store.reserve(agentSessionId, executionId, profile);
  }

  reserveCandidate(input: CandidateReservationInput): string | null {
    return this.#store.reserveCandidate(input);
  }

  cancelReservation(id: string): void {
    this.#store.cancelReservation(id);
  }

  bindAdmission(id: string, taskId: string, inputId: string): void {
    this.#store.bindAdmission(id, taskId, inputId);
  }

  reconcileCandidateResponse(id: string, admission: Admission): void {
    this.#store.reconcileCandidateResponse(id, admission);
  }

  markAborted(id: string, reason: TerminalAbortReason): void {
    this.#store.markAborted(id, reason);
  }

  storeCompletionForAdmission(input: AdmittedTerminalInput): {
    obligationId: string;
    eventId: string;
    bytes: number;
    payloadHash: string;
  } {
    return this.#store.storeCompletionForAdmission(input);
  }

  markAdmissionAborted(agentSessionId: string, executionId: string, reason: TerminalAbortReason): string {
    return this.#store.markAdmissionAborted(agentSessionId, executionId, reason);
  }

  storeCompletion(id: string, input: TerminalEnvelopeInput): { eventId: string; bytes: number; payloadHash: string } {
    return this.#store.storeCompletion(id, input);
  }

  nextWakeAt(): number | undefined {
    return this.#store.nextWakeAt();
  }

  deliver(options: Omit<OutboxDeliveryOptions, "policy">, id?: string): Promise<OutboxDelivery | undefined> {
    return deliverTerminal(this.#store, options, id);
  }

  deliverDue(options: Omit<OutboxDeliveryOptions, "policy">): Promise<void> {
    return deliverTerminalDue(this.#store, options);
  }
}
