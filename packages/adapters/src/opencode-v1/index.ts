export { dispatchOpenCodeV1 } from "./adapter.js";
export {
  ADAPTER,
  ADAPTER_VERSION,
  SUPPORTED_OPENCODE_VERSION,
  TERMINAL_COMPATIBILITY_PROFILE,
  TERMINAL_OUTCOME,
  TERMINAL_SIGNAL,
} from "./contracts.js";
export type {
  CompletionAttempted,
  CompletionNotAttempted,
  CompletionStatus,
  DispatchRequest,
  OpenCodeV1DispatchDependencies,
  OpenCodeV1DispatchOptions,
  OpenCodeV1DispatchResult,
  OpenCodeV1Testing,
  TerminalObservation,
  TerminalObserver,
} from "./contracts.js";
export {
  TerminalOutbox,
  TerminalOutboxController,
  resolveCrucibleDataDir,
  startTerminalOutboxController,
  stopTerminalOutboxController,
} from "../runtime/terminal-outbox.js";
export type {
  AdmittedTerminalInput,
  CandidateReservationInput,
  TerminalAbortReason,
  TerminalOutboxPolicy,
  TerminalOutboxTimer,
} from "../runtime/terminal-outbox.js";
