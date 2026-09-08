import type { Delivery } from "../contracts.js";

export type FetchImpl = (
  input: string,
  init?: RequestInit,
) => Promise<Response>;

export type CandidateInput = {
  agentSessionId: string;
  messageId: string;
  workspacePath: string;
  gitRoot: string;
  projectId: string;
  delivery: Delivery;
  prompt?: string;
  model?: string;
  executionId?: string;
};

export type CoreConnectionOptions = {
  fetchImpl: FetchImpl;
  coreUrl: string;
  timeoutMs: number;
};

export type CanonicalCandidate = {
  eventId: string;
  envelope: string;
  payloadHash: string;
};

export type Admission =
  | {
      tracked: true;
      outcome: "admitted";
      taskId: string | null;
      inputId: string | null;
      eventId: string;
    }
  | {
      tracked: false;
      outcome: string;
      diagnostic: string;
      taskId: string | null;
      inputId: string | null;
      eventId: string;
    };

export type TerminalOutboxTimer = {
  schedule: (callback: () => void, delayMs: number) => unknown;
  cancel: (handle: unknown) => void;
};

export type TerminalOutboxPolicy = {
  maxBytes: number;
  reservationBytes: number;
  authorizationWindowMs: number;
  leaseMs: number;
  backoffBaseMs: number;
  backoffMaxMs: number;
  clock: () => number;
  jitter: (maximumDelayMs: number) => number;
  timer: TerminalOutboxTimer;
  dataDir?: string;
  busyTimeoutMs: number;
};

export type TerminalEnvelopeInput = {
  eventId: string;
  occurredAt: string;
  adapter: string;
  adapterVersion: string;
  agentSessionId: string;
  inputId: string;
  executionId: string;
  projectId: string;
  gitRoot: string;
  workspacePath: string;
  taskId: string;
  terminalSignal: string;
  terminalOutcome: string;
  compatibilityProfile: string;
  terminalObservedAt: string;
};

export type AdmittedTerminalInput = Omit<
  TerminalEnvelopeInput,
  "inputId" | "projectId" | "gitRoot" | "workspacePath" | "taskId"
>;

export type OutboxDeliveryOptions = {
  fetchImpl: FetchImpl;
  coreUrl: string;
  timeoutMs: number;
  policy: TerminalOutboxPolicy;
};

export type CandidateReservationInput = {
  candidate: CanonicalCandidate;
  agentSessionId: string;
  executionId: string;
  projectId: string;
  gitRoot: string;
  workspacePath: string;
  compatibilityProfile: string;
  reconciliationDelayMs: number;
};

export type TerminalAbortReason =
  | "DISPATCH_FAILED"
  | "TERMINAL_OBSERVER_FAILED"
  | "TERMINAL_SIGNAL_MISMATCH";

export type OutboxDelivery = {
  eventId: string;
  terminal: boolean;
  accepted: boolean;
  diagnostic?: string;
};
