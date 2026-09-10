import { createHash, randomUUID } from "node:crypto";

import type {
  Admission,
  CandidateInput,
  CanonicalCandidate,
  CoreConnectionOptions,
  FetchImpl,
} from "./contracts.js";

export type {
  Admission,
  CandidateInput,
  CanonicalCandidate,
  CoreConnectionOptions,
  FetchImpl,
} from "./contracts.js";

export const COMPLETION_UNCONFIRMED = "COMPLETION_UNCONFIRMED";
export const CORE_CONFIGURATION_REQUIRED = "CORE_CONFIGURATION_REQUIRED";

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

function pydanticDatetime(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const match = /^(\d{4}-\d{2}-\d{2})[Tt ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?([Zz]|[+-]\d{2}:?\d{2})$/.exec(value);
  if (!match) return value;
  const micros = match[3]?.slice(0, 6).padEnd(6, "0");
  const fraction = micros && Number(micros) !== 0 ? `.${micros}` : "";
  // Pydantic serializes a zero UTC offset as Z, never as +00:00.
  let timezone = match[4];
  if (timezone.toUpperCase() === "Z") {
    timezone = "Z";
  } else {
    const withColon = timezone.length === 5
      ? `${timezone.slice(0, 3)}:${timezone.slice(3)}`
      : timezone;
    timezone = /^([+-])00:?00$/.test(withColon) ? "Z" : withColon;
  }
  return `${match[1]}T${match[2]}${fraction}${timezone}`;
}

// Lexical PurePosixPath normalization as applied by Pydantic Path on
// POSIX hosts: repeats collapse, single-dot segments resolve, trailing
// separators strip (the root itself stays "/"). Exactly two leading
// slashes are preserved (POSIX implementation-defined double slash);
// three or more collapse to one. ".." is preserved
// lexically (never resolved against the filesystem); backslashes are
// ordinary filename characters and pass through untouched.
export function pydanticPosixPath(value: string): string {
  if (value === "") return ".";
  const rooted = value.startsWith("/");
  const doubleSlashRoot = rooted && value.startsWith("//") && !value.startsWith("///");
  const prefix = doubleSlashRoot ? "//" : rooted ? "/" : "";
  const segments: string[] = [];
  for (const segment of value.split("/")) {
    if (segment === "" || segment === ".") continue;
    segments.push(segment);
  }
  if (segments.length === 0) return rooted ? prefix || "/" : ".";
  return `${prefix}${segments.join("/")}`;
}
// Lexical PureWindowsPath normalization as applied by Pydantic Path on
// Windows: separators unify to backslash, repeats collapse, single-dot
// segments resolve and trailing separators strip. ".." is preserved
// lexically (never resolved against the filesystem).
export function pydanticWindowsPath(value: string): string {
  const slashed = value.replaceAll("/", "\\");
  let prefix = "";
  let rest = slashed;
  if (rest.startsWith("\\\\") && rest[2] !== "\\") {
    prefix = "\\\\";
    rest = rest.slice(2);
  } else {
    const drive = /^[A-Za-z]:/.exec(rest);
    if (drive) {
      prefix = drive[0];
      rest = rest.slice(2);
    }
    if (rest.startsWith("\\")) {
      prefix += "\\";
    }
  }
  const segments: string[] = [];
  for (const segment of rest.split("\\")) {
    if (segment === "" || segment === ".") continue;
    segments.push(segment);
  }
  if (segments.length === 0) return prefix;
  return `${prefix}${prefix.endsWith("\\") || prefix === "" ? "" : "\\"}${segments.join("\\")}`;
}

/** Hash used by Core before transport-byte hashing was introduced. The
 * path normalization follows the platform the adapter runs on, mirroring
 * the Pydantic Path behavior of a Core on the same platform. Only the
 * exact transport SHA-256 or this envelope-derived fallback is accepted
 * downstream — never an arbitrary hash. */
export function legacyNormalizedEnvelopeHash(canonicalEnvelope: string): string {
  const envelope = JSON.parse(canonicalEnvelope) as Record<string, unknown>;
  const normalizePath = (path: unknown): unknown => {
    if (typeof path !== "string") return path;
    return process.platform === "win32" ? pydanticWindowsPath(path) : pydanticPosixPath(path);
  };
  const normalized = {
    ...envelope,
    event_id: typeof envelope["event_id"] === "string"
      ? envelope["event_id"].toLowerCase()
      : envelope["event_id"],
    occurred_at: pydanticDatetime(envelope["occurred_at"]),
    project_id: typeof envelope["project_id"] === "string"
      ? envelope["project_id"].toLowerCase()
      : envelope["project_id"],
    git_root: normalizePath(envelope["git_root"]),
    workspace_path: normalizePath(envelope["workspace_path"]),
  };
  return createHash("sha256").update(canonicalJson(normalized)).digest("hex");
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
