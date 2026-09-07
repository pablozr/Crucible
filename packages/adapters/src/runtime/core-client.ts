import { createHash, randomUUID } from "node:crypto";

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

export const COMPLETION_UNCONFIRMED = "COMPLETION_UNCONFIRMED";
export const CORE_CONFIGURATION_REQUIRED = "CORE_CONFIGURATION_REQUIRED";

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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function jsonString(value: string): string {
  return JSON.stringify(value).replace(/[^\x00-\x7f]/g, (character) =>
    `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${jsonString(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }
  return typeof value === "string" ? jsonString(value) : JSON.stringify(value);
}

export function createCanonicalCandidate(
  input: CandidateInput,
  options: { adapter: string; adapterVersion: string; eventId?: string; occurredAt?: string },
): CanonicalCandidate {
  const eventId = options.eventId ?? randomUUID();
  const payload: Record<string, unknown> = { delivery: input.delivery };
  if (input.prompt !== undefined) payload["prompt"] = input.prompt;
  if (input.model !== undefined) payload["model"] = input.model;

  const envelope = canonicalJson({
    adapter: options.adapter,
    adapter_version: options.adapterVersion,
    agent_session_id: input.agentSessionId,
    event_id: eventId,
    event_type: "input_candidate",
    ...(input.executionId ? { execution_id: input.executionId } : {}),
    git_root: input.gitRoot,
    input_id: input.messageId,
    occurred_at: options.occurredAt ?? new Date().toISOString(),
    payload,
    payload_version: 1,
    project_id: input.projectId,
    workspace_path: input.workspacePath,
  });

  return {
    eventId,
    envelope,
    payloadHash: createHash("sha256").update(envelope).digest("hex"),
  };
}

function asEventAdmission(
  body: unknown,
  eventId: string,
): Admission | undefined {
  if (!isRecord(body) || body["status"] !== "ok" || !isRecord(body["data"])) {
    return undefined;
  }

  const data = body["data"] as Record<string, unknown>;
  if (!isRecord(data["event"])) {
    return undefined;
  }

  const event = data["event"] as Record<string, unknown>;
  if (
    typeof event["outcome"] !== "string" ||
    typeof event["dispatch_authorized"] !== "boolean" ||
    typeof event["event_id"] !== "string" ||
    (event["event_id"] as string) !== eventId
  ) {
    return undefined;
  }

  const outcome = event["outcome"] as string;
  const taskId =
    typeof event["task_id"] === "string" ? (event["task_id"] as string) : null;
  const inputId =
    typeof event["input_id"] === "string"
      ? (event["input_id"] as string)
      : null;

  if (outcome === "admitted" && event["dispatch_authorized"] === true) {
    if (!taskId || !inputId) {
      return undefined;
    }

    return { tracked: true, outcome, taskId, inputId, eventId };
  }

  return {
    tracked: false,
    outcome,
    diagnostic: outcome,
    taskId,
    inputId,
    eventId,
  };
}

function asErrorAdmission(body: unknown, eventId: string): Admission | undefined {
  if (
    !isRecord(body) ||
    body["status"] !== "error" ||
    !isRecord(body["data"]) ||
    typeof body["data"]["code"] !== "string"
  ) {
    return undefined;
  }

  const code = body["data"]["code"] as string;
  return {
    tracked: false,
    outcome: code,
    diagnostic: code,
    taskId: null,
    inputId: null,
    eventId,
  };
}

export async function postInputCandidate(
  input: CandidateInput,
  options: {
    fetchImpl: FetchImpl;
    adapter: string;
    adapterVersion: string;
    coreUrl?: string;
    timeoutMs?: number;
    eventId?: string;
  },
): Promise<Admission> {
  const candidate = createCanonicalCandidate(input, options);
  return postCanonicalCandidate(candidate, options);
}

export async function postCanonicalCandidate(
  candidate: CanonicalCandidate,
  options: {
    fetchImpl: FetchImpl;
    coreUrl?: string;
    timeoutMs?: number;
  },
): Promise<Admission> {
  const { eventId, envelope } = candidate;
  if (!options.coreUrl || !options.timeoutMs) {
    return {
      tracked: false,
      outcome: "tracking_skipped",
      diagnostic: CORE_CONFIGURATION_REQUIRED,
      taskId: null,
      inputId: null,
      eventId,
    };
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs);
  let response: Response;

  try {
    response = await options.fetchImpl(`${options.coreUrl}/v1/events`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: envelope,
      signal: controller.signal,
    });
  } catch {
    return {
      tracked: false,
      outcome: "tracking_skipped",
      diagnostic: "CORE_UNAVAILABLE",
      taskId: null,
      inputId: null,
      eventId,
    };
  } finally {
    clearTimeout(timer);
  }

  let parsed: unknown;

  try {
    parsed = await response.json();
  } catch {
    return {
      tracked: false,
      outcome: "tracking_skipped",
      diagnostic: "CORE_UNAVAILABLE",
      taskId: null,
      inputId: null,
      eventId,
    };
  }

  if (response.ok) {
    const admission = asEventAdmission(parsed, eventId);

    if (!admission) {
      return {
        tracked: false,
        outcome: "tracking_skipped",
        diagnostic: "CORE_UNAVAILABLE",
        taskId: null,
        inputId: null,
        eventId,
      };
    }

    return admission;
  }

  const errorAdmission = asErrorAdmission(parsed, eventId);

  if (!errorAdmission) {
    return {
      tracked: false,
      outcome: "tracking_skipped",
      diagnostic: "CORE_UNAVAILABLE",
      taskId: null,
      inputId: null,
      eventId,
    };
  }

  return errorAdmission;
}

async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  options: CoreConnectionOptions,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs);
  try {
    return await options.fetchImpl(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

export async function postCanonicalEvent(
  canonicalEnvelope: string,
  options: CoreConnectionOptions,
): Promise<unknown> {
  const response = await fetchWithTimeout(
    `${options.coreUrl}/v1/events`,
    { method: "POST", headers: { "content-type": "application/json" }, body: canonicalEnvelope },
    options,
  );
  return response.json();
}

export async function getEvent(
  eventId: string,
  options: CoreConnectionOptions,
): Promise<{ status: number; body: unknown }> {
  const response = await fetchWithTimeout(
    `${options.coreUrl}/v1/events/${eventId}`,
    { method: "GET" },
    options,
  );
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }
  return { status: response.status, body };
}
