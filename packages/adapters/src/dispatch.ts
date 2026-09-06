import { isAbsolute } from "node:path";

import { postInputCandidate } from "./core-client.js";
import { resolveProject as defaultResolveProject } from "./project.js";
import {
  SUPPORTED_OPENCODE_VERSION,
  type DispatchDependencies,
  type DispatchOptions,
  type DispatchRequest,
  type DispatchResult,
} from "./types.js";

function errorCode(error: unknown, fallback: string): string {
  if (error instanceof Error) {
    const prefix = error.message.split(":")[0]?.trim();
    if (prefix && /^[A-Z0-9_]+$/.test(prefix)) {
      return prefix;
    }
  }
  return fallback;
}

export async function dispatchOpenCodeV1<T>(
  request: DispatchRequest,
  dependencies: DispatchDependencies<T>,
  options: DispatchOptions = {},
): Promise<DispatchResult<T>> {
  const fetchImpl =
    dependencies.fetchImpl ?? (globalThis.fetch as typeof fetch);
  const resolveProject = dependencies.resolveProject ?? defaultResolveProject;

  if (request.openCodeVersion !== SUPPORTED_OPENCODE_VERSION) {
    const context = {
      tracked: false as const,
      outcome: "tracking_skipped",
      diagnostic: "INCOMPATIBLE_OPENCODE",
      taskId: null,
      inputId: null,
      eventId: options.eventId ?? request.messageId,
    };
    const dispatchResult = await dependencies.dispatch(context);
    return { ...context, dispatchResult };
  }

  if (
    !request.agentSessionId ||
    !request.messageId ||
    !request.workspacePath ||
    !isAbsolute(request.workspacePath) ||
    (request.delivery !== "new" && request.delivery !== "steer")
  ) {
    const diagnostic =
      !isAbsolute(request.workspacePath ?? "")
        ? "PATH_MUST_BE_ABSOLUTE"
        : "INVALID_DISPATCH_REQUEST";
    const context = {
      tracked: false as const,
      outcome: "tracking_skipped",
      diagnostic,
      taskId: null,
      inputId: null,
      eventId: options.eventId ?? request.messageId,
    };
    const dispatchResult = await dependencies.dispatch(context);
    return { ...context, dispatchResult };
  }

  let project: { projectId: string; gitRoot: string };
  try {
    project = await resolveProject(request.workspacePath);
  } catch (error) {
    const context = {
      tracked: false as const,
      outcome: "tracking_skipped",
      diagnostic: errorCode(error, "UNINITIALIZED_PROJECT"),
      taskId: null,
      inputId: null,
      eventId: options.eventId ?? request.messageId,
    };
    const dispatchResult = await dependencies.dispatch(context);
    return { ...context, dispatchResult };
  }

  const admission = await postInputCandidate(
    {
      agentSessionId: request.agentSessionId,
      messageId: request.messageId,
      workspacePath: request.workspacePath,
      gitRoot: project.gitRoot,
      projectId: project.projectId,
      delivery: request.delivery,
      prompt: request.prompt,
      model: request.model,
    },
    {
      fetchImpl,
      coreUrl: options.coreUrl,
      timeoutMs: options.timeoutMs,
      eventId: options.eventId,
      adapterVersion: options.adapterVersion,
    },
  );

  const context =
    admission.tracked === true
      ? {
          tracked: true as const,
          outcome: admission.outcome,
          taskId: admission.taskId,
          inputId: admission.inputId,
          eventId: admission.eventId,
        }
      : {
          tracked: false as const,
          outcome: admission.outcome,
          diagnostic: admission.diagnostic,
          taskId: admission.taskId,
          inputId: admission.inputId,
          eventId: admission.eventId,
        };

  const dispatchResult = await dependencies.dispatch(context);
  return { ...context, dispatchResult };
}
