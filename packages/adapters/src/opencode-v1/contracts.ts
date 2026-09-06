import type { Delivery, DispatchFn } from "../contracts.js";
import type { FetchImpl } from "../runtime/core-client.js";
import type { ResolveProjectFn } from "../runtime/project-resolver.js";

export const SUPPORTED_OPENCODE_VERSION = "1.18.28";
export const ADAPTER = "opencode-v1";
export const ADAPTER_VERSION = "0.1.0";

export type DispatchRequest = {
  openCodeVersion: string;
  agentSessionId: string;
  messageId: string;
  workspacePath: string;
  delivery: Delivery;
  prompt?: string;
  model?: string;
};

export type OpenCodeV1DispatchDependencies<T> = {
  dispatch: DispatchFn<T>;
};

export type OpenCodeV1Testing = {
  fetchImpl?: FetchImpl;
  resolveProject?: ResolveProjectFn;
  eventId?: string;
};

export type OpenCodeV1DispatchOptions = {
  coreUrl?: string;
  timeoutMs?: number;
  adapterVersion?: string;
  testing?: OpenCodeV1Testing;
};
