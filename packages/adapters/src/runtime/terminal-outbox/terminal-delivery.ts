import {
  COMPLETION_UNCONFIRMED,
  getEvent,
  legacyNormalizedEnvelopeHash,
  postCanonicalEvent,
} from "../core-client.js";
import type {
  CoreConnectionOptions,
  OutboxDelivery,
  OutboxDeliveryOptions,
} from "../contracts.js";
import type {
  CandidateRow,
  OutboxRow,
  TerminalOutboxStore,
} from "./terminal-outbox-store.js";

export function responseEvent(body: unknown): Record<string, unknown> | undefined {
  if (!body || typeof body !== "object" || Array.isArray(body)) return undefined;
  const data = (body as Record<string, unknown>)["data"];
  if (!data || typeof data !== "object" || Array.isArray(data)) return undefined;
  const event = (data as Record<string, unknown>)["event"];
  return event && typeof event === "object" && !Array.isArray(event)
    ? (event as Record<string, unknown>)
    : undefined;
}

export function compatiblePayloadHash(
  actual: unknown,
  transportHash: string | null,
  envelope: string | null,
): boolean {
  return typeof actual === "string" && transportHash !== null && envelope !== null &&
    (actual === transportHash || actual === legacyNormalizedEnvelopeHash(envelope));
}

export function terminalResponseForRow(
  row: OutboxRow,
  body: unknown,
  requirePayloadHash: boolean,
): OutboxDelivery | undefined {
  const abortMode = row.abort_envelope !== null && row.abort_envelope !== undefined;
  const expectedEventId = abortMode ? row.abort_event_id : row.event_id;
  const expectedHash = abortMode ? row.abort_payload_hash : row.payload_hash;
  const expectedEnvelope = abortMode ? row.abort_envelope : row.envelope;
  const event = responseEvent(body);
  if (!event || event["event_id"] !== expectedEventId || event["task_id"] !== row.task_id ||
      event["input_id"] !== row.input_id || event["dispatch_authorized"] !== false ||
      (requirePayloadHash && !compatiblePayloadHash(
        event["payload_hash"],
        expectedHash,
        expectedEnvelope,
      ))) {
    return undefined;
  }
  if (abortMode) {
    if (event["status"] !== "rejected" || event["outcome"] !== "rejected") return undefined;
    return {
      eventId: expectedEventId!,
      terminal: true,
      accepted: false,
      diagnostic: row.abort_reason ?? "ABORT_ACKNOWLEDGED",
    };
  }
  if (event["status"] !== "accepted" && event["status"] !== "rejected") return undefined;
  if (
    (event["status"] === "accepted" && event["outcome"] !== "completed") ||
    (event["status"] === "rejected" && event["outcome"] !== "rejected")
  ) {
    return undefined;
  }
  return {
    eventId: row.event_id!,
    terminal: true,
    accepted: event["status"] === "accepted",
    diagnostic: event["status"] === "rejected"
      ? String(event["failure_code"] ?? "COMPLETION_REJECTED")
      : undefined,
  };
}

export async function reconcileCandidate(
  store: TerminalOutboxStore,
  options: CoreConnectionOptions,
  row: CandidateRow,
): Promise<void> {
  try {
    const response = await getEvent(row.candidate_event_id, options);
    if (response.status === 404) {
      const data = responseEvent(response.body);
      const errorData = response.body && typeof response.body === "object"
        ? (response.body as Record<string, unknown>)["data"]
        : undefined;
      if (!data && errorData && typeof errorData === "object" &&
          (errorData as Record<string, unknown>)["code"] === "EVENT_NOT_FOUND") {
        if (row.last_diagnostic === "EVENT_NOT_FOUND") {
          store.releaseCandidate(row.id, "EVENT_NOT_FOUND");
        } else {
          store.retryCandidate(row, "EVENT_NOT_FOUND");
        }
        return;
      }
      store.retryCandidate(row, "CANDIDATE_RECONCILIATION_UNCONFIRMED");
      return;
    }
    const event = responseEvent(response.body);
    if (!event || event["event_id"] !== row.candidate_event_id ||
        !compatiblePayloadHash(
          event["payload_hash"],
          row.candidate_payload_hash,
          row.candidate_envelope,
        )) {
      store.retryCandidate(row, "CANDIDATE_RECONCILIATION_MISMATCH");
      return;
    }
    if (event["status"] === "accepted" && event["outcome"] === "admitted" &&
        event["dispatch_authorized"] === true && typeof event["task_id"] === "string" &&
        typeof event["input_id"] === "string") {
      store.bindAdmission(row.id, event["task_id"], event["input_id"]);
    } else if (event["status"] === "rejected" && event["dispatch_authorized"] === false) {
      store.releaseCandidate(row.id, String(event["failure_code"] ?? event["outcome"] ?? "CANDIDATE_REJECTED"));
    } else {
      store.retryCandidate(row, "CANDIDATE_RECONCILIATION_PENDING");
    }
  } catch {
    store.retryCandidate(row, "CANDIDATE_RECONCILIATION_UNCONFIRMED");
  }
}

// Core transport for terminal obligations: POST first, then GET reconcile on a
// lost response. The reservation is persisted before the POST and the ACK is
// transactional in the store, so a crash between them stays recoverable.
export async function deliverTerminal(
  store: TerminalOutboxStore,
  options: Omit<OutboxDeliveryOptions, "policy">,
  id?: string,
): Promise<OutboxDelivery | undefined> {
  const row = store.leaseDelivery(id);
  if (!row) return undefined;
  const abortMode = row.abort_envelope !== null && row.abort_envelope !== undefined;
  const eventId = abortMode ? row.abort_event_id : row.event_id;
  const envelope = abortMode ? row.abort_envelope : row.envelope;
  if (!eventId || !envelope) return undefined;
  let postBody: unknown;
  try {
    postBody = await postCanonicalEvent(envelope, options);
    const direct = terminalResponseForRow(row, postBody, false);
    if (direct) return store.finishDelivery(row, direct);
  } catch {
    // A lost response can still represent a committed Core event; reconcile below.
  }

  try {
    const detail = await getEvent(eventId, options);
    const reconciled = detail.status === 200
      ? terminalResponseForRow(row, detail.body, true)
      : undefined;
    if (reconciled) return store.finishDelivery(row, reconciled);
  } catch {
    // Reachable not-found and transport failures both remain retryable.
  }
  return store.retryDelivery(row, COMPLETION_UNCONFIRMED);
}

export async function deliverTerminalDue(
  store: TerminalOutboxStore,
  options: Omit<OutboxDeliveryOptions, "policy">,
): Promise<void> {
  for (const candidate of store.listDueCandidates()) {
    await reconcileCandidate(store, options, candidate);
  }
  for (const row of store.listDueDeliveryIds()) {
    await deliverTerminal(store, options, row.id);
  }
}
