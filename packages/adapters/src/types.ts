export const SUPPORTED_OPENCODE_VERSION = "1.18.28";
export const ADAPTER = "opencode-v1";
export const ADAPTER_VERSION = "0.1.0";
export const DEFAULT_CORE_URL = "http://127.0.0.1:7331";
export const DEFAULT_TIMEOUT_MS = 2000;

export type Delivery = "new" | "steer";

export type DispatchRequest = {
  openCodeVersion: string;
  agentSessionId: string;
  messageId: string;
  workspacePath: string;
  delivery: Delivery;
  prompt?: string;
  model?: string;
};

export type ResolvedProject = {
  projectId: string;
  gitRoot: string;
};

export type DispatchContext = {
  tracked: boolean;
  outcome: string;
  diagnostic?: string;
  taskId: string | null;
  inputId: string | null;
  eventId: string;
};

export type DispatchResult<T> = DispatchContext & {
  dispatchResult: T;
};

export type DispatchFn<T> = (context: DispatchContext) => Promise<T> | T;

export type ResolveProjectFn = (
  workspacePath: string,
) => ResolvedProject | Promise<ResolvedProject>;

export type FetchImpl = (
  input: string,
  init?: RequestInit,
) => Promise<Response>;

export type DispatchDependencies<T> = {
  dispatch: DispatchFn<T>;
  fetchImpl?: FetchImpl;
  resolveProject?: ResolveProjectFn;
};

export type DispatchOptions = {
  coreUrl?: string;
  timeoutMs?: number;
  eventId?: string;
  adapterVersion?: string;
};
