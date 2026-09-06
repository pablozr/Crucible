import { randomUUID } from "node:crypto";

import {
  ADAPTER,
  ADAPTER_VERSION,
  DEFAULT_CORE_URL,
  DEFAULT_TIMEOUT_MS,
  type Delivery,
  type FetchImpl,
} from "./types.js";

export type CandidateInput = {
  agentSessionId: string;
  messageId: string;
  workspacePath: string;
  gitRoot: string;
  projectId: string;
  delivery: Delivery;
  prompt?: string;
  model?: string;
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
    typeof event["dispatch_authorized"] !== "boolean"
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
    coreUrl?: string;
    timeoutMs?: number;
    eventId?: string;
    adapterVersion?: string;
  },
): Promise<Admission> {
  const eventId = options.eventId ?? randomUUID();
  const coreUrl = options.coreUrl ?? DEFAULT_CORE_URL;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const fetchImpl = options.fetchImpl;

  const payload: Record<string, unknown> = { delivery: input.delivery };
  if (input.prompt !== undefined) {
    payload["prompt"] = input.prompt;
  }
  if (input.model !== undefined) {
    payload["model"] = input.model;
  }

  const body = {
    event_id: eventId,
    event_type: "input_candidate",
    occurred_at: new Date().toISOString(),
    payload_version: 1,
    adapter: ADAPTER,
    adapter_version: options.adapterVersion ?? ADAPTER_VERSION,
    agent_session_id: input.agentSessionId,
    input_id: input.messageId,
    project_id: input.projectId,
    git_root: input.gitRoot,
    workspace_path: input.workspacePath,
    payload,
  };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response: Response;
  try {
    response = await fetchImpl(`${coreUrl}/v1/events`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
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
