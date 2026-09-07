import { isAbsolute } from "node:path";
import { randomUUID } from "node:crypto";

import type { DispatchResult, UntrackedDispatchContext } from "../contracts.js";
import { postInputCandidate } from "../runtime/core-client.js";
import { resolveProject as defaultResolveProject } from "../runtime/project-resolver.js";
import { runTrackedDispatch } from "../runtime/tracked-dispatch.js";
import {
  ADAPTER,
  ADAPTER_VERSION,
  SUPPORTED_OPENCODE_VERSION,
  type DispatchRequest,
  type OpenCodeV1DispatchDependencies,
  type OpenCodeV1DispatchOptions,
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

export async function dispatchOpenCodeV1<T>(
  request: DispatchRequest,
  dependencies: OpenCodeV1DispatchDependencies<T>,
  options: OpenCodeV1DispatchOptions = {},
): Promise<DispatchResult<T>> {
  const testing = options.testing ?? {};
  const fetchImpl =
    testing.fetchImpl ?? (globalThis.fetch as typeof fetch);
  const resolveProject = testing.resolveProject ?? defaultResolveProject;
  const eventId = testing.eventId ?? randomUUID();

  if (request.openCodeVersion !== SUPPORTED_OPENCODE_VERSION) {
    const context = skippedContext("INCOMPATIBLE_OPENCODE", eventId);

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
    const diagnostic = !isAbsolute(request.workspacePath ?? "")
      ? "PATH_MUST_BE_ABSOLUTE"
      : "INVALID_DISPATCH_REQUEST";
    const context = skippedContext(diagnostic, eventId);

    const dispatchResult = await dependencies.dispatch(context);
    return { ...context, dispatchResult };
  }

  const coreOptions = {
    fetchImpl,
    coreUrl: options.coreUrl,
    timeoutMs: options.timeoutMs,
    eventId,
    adapter: ADAPTER,
    adapterVersion: options.adapterVersion ?? ADAPTER_VERSION,
  };

  const postCandidate = (project: { projectId: string; gitRoot: string }) => {
    const candidate = {
      agentSessionId: request.agentSessionId,
      messageId: request.messageId,
      workspacePath: request.workspacePath,
      gitRoot: project.gitRoot,
      projectId: project.projectId,
      delivery: request.delivery,
      prompt: request.prompt,
      model: request.model,
    };

    return postInputCandidate(candidate, coreOptions);
  };

  return runTrackedDispatch(
    {
      eventId,
      resolveProject: () => resolveProject(request.workspacePath),
      postCandidate,
    },
    dependencies,
  );
}
