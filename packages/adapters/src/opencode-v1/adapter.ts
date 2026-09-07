import { isAbsolute } from "node:path";
import { randomUUID } from "node:crypto";

import type { UntrackedDispatchContext } from "../contracts.js";
import {
  COMPLETION_UNCONFIRMED,
  CORE_CONFIGURATION_REQUIRED,
  createCanonicalCandidate,
  postCanonicalCandidate,
} from "../runtime/core-client.js";
import { resolveProject as defaultResolveProject } from "../runtime/project-resolver.js";
import { runTrackedDispatch } from "../runtime/tracked-dispatch.js";
import { startTerminalOutboxController, TERMINAL_ABORT_PERSISTED } from "../runtime/terminal-outbox.js";
import {
  ADAPTER,
  ADAPTER_VERSION,
  SUPPORTED_OPENCODE_VERSION,
  TERMINAL_COMPATIBILITY_PROFILE,
  TERMINAL_OUTCOME,
  TERMINAL_SIGNAL,
  type CompletionStatus,
  type DispatchRequest,
  type OpenCodeV1DispatchDependencies,
  type OpenCodeV1DispatchOptions,
  type OpenCodeV1DispatchResult,
  type TerminalObservation,
} from "./contracts.js";

function skippedContext(
  diagnostic: string,
  eventId: string,
): UntrackedDispatchContext {
  return {
    tracked: false as const,
    outcome: "tracking_skipped",
    diagnostic,
    taskId: null,
    inputId: null,
    eventId,
  };
}

function notAttempted(reason: string): CompletionStatus {
  return { attempted: false as const, reason, eventId: null };
}

function isExactTerminal(
  observation: TerminalObservation | null | undefined,
  request: DispatchRequest,
): observation is TerminalObservation & { observedAt: string } {
  return (
    !!observation &&
    observation.agentSessionId === request.agentSessionId &&
    observation.executionId === request.executionId &&
    typeof observation.observedAt === "string" &&
    Number.isFinite(Date.parse(observation.observedAt)) &&
    observation.signal === TERMINAL_SIGNAL &&
    observation.finish === TERMINAL_OUTCOME
  );
}

export async function dispatchOpenCodeV1<T>(
  request: DispatchRequest,
  dependencies: OpenCodeV1DispatchDependencies<T>,
  options: OpenCodeV1DispatchOptions = {},
): Promise<OpenCodeV1DispatchResult<T>> {
  const testing = options.testing ?? {};
  const fetchImpl =
    testing.fetchImpl ?? (globalThis.fetch as typeof fetch);
  const resolveProject = testing.resolveProject ?? defaultResolveProject;
  const eventId = testing.eventId ?? randomUUID();
  const coreUrl = options.coreUrl ?? (testing.fetchImpl ? "http://core.test" : undefined);
  const timeoutMs = options.timeoutMs ?? (testing.fetchImpl ? 2000 : undefined);

  if (request.openCodeVersion !== SUPPORTED_OPENCODE_VERSION) {
    const context = skippedContext("INCOMPATIBLE_OPENCODE", eventId);

    const dispatchResult = await dependencies.dispatch(context);
    return { ...context, dispatchResult, completion: notAttempted("UNTRACKED_DISPATCH") };
  }

  if (
    !request.agentSessionId ||
    !request.messageId ||
    !request.workspacePath ||
    !isAbsolute(request.workspacePath) ||
    (request.delivery !== "new" && request.delivery !== "steer")
  ) {
    const diagnostic = !isAbsolute(request.workspacePath ?? "")
      ? "PATH_MUST_BE_ABSOLUTE"
      : "INVALID_DISPATCH_REQUEST";
    const context = skippedContext(diagnostic, eventId);

    const dispatchResult = await dependencies.dispatch(context);
    return { ...context, dispatchResult, completion: notAttempted("UNTRACKED_DISPATCH") };
  }

  const terminalEnabled =
    options.compatibilityProfile === TERMINAL_COMPATIBILITY_PROFILE &&
    (options.terminalPolicy !== undefined || options.terminalController !== undefined);
  if (terminalEnabled && !request.executionId.trim()) {
    const context = skippedContext("INVALID_DISPATCH_REQUEST", eventId);
    const dispatchResult = await dependencies.dispatch(context);
    return { ...context, dispatchResult, completion: notAttempted("UNTRACKED_DISPATCH") };
  }
  if (terminalEnabled && !options.observeTerminal) {
    const context = skippedContext("TERMINAL_OBSERVER_REQUIRED", eventId);
    const dispatchResult = await dependencies.dispatch(context);
    return { ...context, dispatchResult, completion: notAttempted("TERMINAL_SIGNAL_MISMATCH") };
  }

  const adapterVersion = options.adapterVersion ?? ADAPTER_VERSION;
  const executionId = request.executionId;
  if (!coreUrl || !timeoutMs) {
    const context = skippedContext(CORE_CONFIGURATION_REQUIRED, eventId);
    const dispatchResult = await dependencies.dispatch(context);
    return { ...context, dispatchResult, completion: notAttempted("UNTRACKED_DISPATCH") };
  }

  let outbox = options.terminalController;
  if (terminalEnabled && !outbox) {
    try {
      outbox = startTerminalOutboxController(options.terminalPolicy!, {
        fetchImpl,
        coreUrl,
        timeoutMs,
      });
    } catch {
      const context = skippedContext("TERMINAL_CONTROLLER_UNAVAILABLE", eventId);
      const dispatchResult = await dependencies.dispatch(context);
      return { ...context, dispatchResult, completion: notAttempted("UNTRACKED_DISPATCH") };
    }
  }
  try {
    outbox?.start();
  } catch {
    const context = skippedContext("TERMINAL_CONTROLLER_UNAVAILABLE", eventId);
    const dispatchResult = await dependencies.dispatch(context);
    return { ...context, dispatchResult, completion: notAttempted("UNTRACKED_DISPATCH") };
  }

  const coreOptions = {
    fetchImpl,
    coreUrl,
    timeoutMs,
    eventId,
    adapter: ADAPTER,
    adapterVersion,
  };

  let reservationId: string | null = null;

  const postCandidate = async (project: { projectId: string; gitRoot: string }) => {
    const candidate = {
      agentSessionId: request.agentSessionId,
      messageId: request.messageId,
      workspacePath: request.workspacePath,
      gitRoot: project.gitRoot,
      projectId: project.projectId,
      delivery: request.delivery,
      prompt: request.prompt,
      model: request.model,
      executionId,
    };
    const canonicalCandidate = createCanonicalCandidate(candidate, coreOptions);
    if (outbox) {
      reservationId = outbox.reserveCandidate({
        candidate: canonicalCandidate,
        agentSessionId: request.agentSessionId,
        executionId,
        projectId: project.projectId,
        gitRoot: project.gitRoot,
        workspacePath: request.workspacePath,
        compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
        reconciliationDelayMs: timeoutMs,
      });
      if (!reservationId) {
        return {
          tracked: false as const,
          outcome: "tracking_skipped",
          diagnostic: "TERMINAL_OUTBOX_CAPACITY_EXHAUSTED",
          taskId: null,
          inputId: null,
          eventId,
        };
      }
    }
    const admission = await postCanonicalCandidate(canonicalCandidate, coreOptions);
    if (outbox && reservationId) {
      outbox.reconcileCandidateResponse(reservationId, admission);
      if (admission.tracked !== true) reservationId = null;
    }
    return admission;
  };

  let resolvedProject: { projectId: string; gitRoot: string } | undefined;

  const capturingResolve = async () => {
    const resolved = await resolveProject(request.workspacePath);
    resolvedProject = resolved;
    return resolved;
  };

  let dispatchOutcome: Awaited<ReturnType<typeof runTrackedDispatch<T>>>;
  try {
    dispatchOutcome = await runTrackedDispatch(
      {
        eventId,
        resolveProject: capturingResolve,
        postCandidate,
      },
      dependencies,
    );
  } catch (error) {
    if (outbox && reservationId) {
      try {
        outbox.markAdmissionAborted(
          request.agentSessionId,
          executionId,
          "DISPATCH_FAILED",
        );
      } catch {
        // The original dispatch failure remains authoritative; the durable row is retained.
      }
    }
    throw error;
  }

  if (dispatchOutcome.tracked !== true || !dispatchOutcome.taskId || !dispatchOutcome.inputId) {
    return { ...dispatchOutcome, completion: notAttempted("UNTRACKED_DISPATCH") };
  }

  if (!terminalEnabled || !outbox || !reservationId) {
    return {
      ...dispatchOutcome,
      completion: notAttempted("COMPATIBILITY_PROFILE_MISMATCH"),
    };
  }

  let observation: TerminalObservation | null | undefined;

  try {
    observation = await options.observeTerminal!();
  } catch {
    try {
      outbox.markAdmissionAborted(request.agentSessionId, executionId, "TERMINAL_OBSERVER_FAILED");
    } catch {
      // The admitted row remains recoverable if abandonment persistence fails.
    }
    return {
      ...dispatchOutcome,
      completion: notAttempted("TERMINAL_SIGNAL_MISMATCH"),
    };
  }

  if (!isExactTerminal(observation, request)) {
    try {
      outbox.markAdmissionAborted(request.agentSessionId, executionId, "TERMINAL_SIGNAL_MISMATCH");
    } catch {
      // The admitted row remains recoverable if abandonment persistence fails.
    }
    return {
      ...dispatchOutcome,
      completion: notAttempted("TERMINAL_SIGNAL_MISMATCH"),
    };
  }

  if (!resolvedProject) {
    return { ...dispatchOutcome, completion: notAttempted("UNTRACKED_DISPATCH") };
  }

  let completionEventId = testing.completionEventId ?? randomUUID();
  if (completionEventId === eventId) {
    completionEventId = randomUUID();
  }

  let completion: CompletionStatus;

  try {
    outbox.storeCompletion(reservationId, {
      eventId: completionEventId,
      occurredAt: observation.observedAt,
      adapter: ADAPTER,
      adapterVersion,
      agentSessionId: request.agentSessionId,
      inputId: request.messageId,
      executionId,
      projectId: resolvedProject.projectId,
      gitRoot: resolvedProject.gitRoot,
      workspacePath: request.workspacePath,
      taskId: dispatchOutcome.taskId,
      terminalSignal: TERMINAL_SIGNAL,
      terminalOutcome: TERMINAL_OUTCOME,
      compatibilityProfile: TERMINAL_COMPATIBILITY_PROFILE,
      terminalObservedAt: observation.observedAt,
    });
    const confirmation = await outbox.deliver(reservationId);

    if (confirmation?.terminal) {
      completion = {
        attempted: true as const,
        confirmed: confirmation.accepted,
        eventId: completionEventId,
        taskId: dispatchOutcome.taskId,
        ...(confirmation.diagnostic ? { diagnostic: confirmation.diagnostic } : {}),
      };
    } else {
      completion = {
        attempted: true as const,
        confirmed: false,
        eventId: completionEventId,
        diagnostic: COMPLETION_UNCONFIRMED,
      };
    }
  } catch (error) {
    completion =
      error instanceof Error && error.message === TERMINAL_ABORT_PERSISTED
        ? notAttempted(TERMINAL_ABORT_PERSISTED)
        : {
            attempted: true as const,
            confirmed: false,
            eventId: completionEventId,
            diagnostic: COMPLETION_UNCONFIRMED,
          };
  }

  return { ...dispatchOutcome, completion };
}
