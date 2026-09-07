import type { Delivery, DispatchFn, DispatchResult } from "../contracts.js";
import type { FetchImpl } from "../runtime/core-client.js";
import type { ResolveProjectFn } from "../runtime/project-resolver.js";
import type {
  TerminalOutboxController,
  TerminalOutboxPolicy,
} from "../runtime/terminal-outbox.js";

export const SUPPORTED_OPENCODE_VERSION = "1.18.28";
export const ADAPTER = "opencode-v1";
export const ADAPTER_VERSION = "0.1.0";

export const TERMINAL_COMPATIBILITY_PROFILE =
  "opencode-v1-1.18.28-write-stop-restricted";
export const TERMINAL_SIGNAL = "session_prompt_return";
export const TERMINAL_OUTCOME = "stop";

export type DispatchRequest = {
  openCodeVersion: string;
  agentSessionId: string;
  messageId: string;
  executionId: string;
  workspacePath: string;
  delivery: Delivery;
  prompt?: string;
  model?: string;
};

export type OpenCodeV1DispatchDependencies<T> = {
  dispatch: DispatchFn<T>;
};

export type TerminalObservation = {
  agentSessionId: string;
  executionId: string;
  observedAt: string;
  signal: string;
  finish: string;
};

export type TerminalObserver = () =>
  | TerminalObservation
  | null
  | undefined
  | Promise<TerminalObservation | null | undefined>;

export type CompletionNotAttempted = {
  attempted: false;
  reason: string;
  eventId: null;
};

export type CompletionAttempted = {
  attempted: true;
  confirmed: boolean;
  eventId: string;
  taskId?: string | null;
  diagnostic?: string;
};

export type CompletionStatus = CompletionNotAttempted | CompletionAttempted;

export type OpenCodeV1DispatchResult<T> = DispatchResult<T> & {
  completion: CompletionStatus;
};

export type OpenCodeV1Testing = {
  fetchImpl?: FetchImpl;
  resolveProject?: ResolveProjectFn;
  eventId?: string;
  completionEventId?: string;
};

export type OpenCodeV1DispatchOptions = {
  coreUrl?: string;
  timeoutMs?: number;
  adapterVersion?: string;
  compatibilityProfile?: string;
  observeTerminal?: TerminalObserver;
  terminalPolicy?: TerminalOutboxPolicy;
  terminalController?: TerminalOutboxController;
  testing?: OpenCodeV1Testing;
};
