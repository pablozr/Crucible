import type {
  DispatchContext,
  DispatchFn,
  DispatchResult,
  UntrackedDispatchContext,
} from "../contracts.js";
import type { Admission } from "./contracts.js";
import type { ResolvedProject } from "./project-resolver.js";

function errorCode(error: unknown, fallback: string): string {
  if (error instanceof Error) {
    const prefix = error.message.split(":")[0]?.trim();
    if (prefix && /^[A-Z0-9_]+$/.test(prefix)) {
      return prefix;
    }
  }
  return fallback;
}

export function toDispatchContext(admission: Admission): DispatchContext {
  if (admission.tracked === true) {
    return {
      tracked: true as const,
      outcome: admission.outcome,
      taskId: admission.taskId,
      inputId: admission.inputId,
      eventId: admission.eventId,
    };
  }

  return {
    tracked: false as const,
    outcome: admission.outcome,
    diagnostic: admission.diagnostic,
    taskId: admission.taskId,
    inputId: admission.inputId,
    eventId: admission.eventId,
  };
}

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

export async function runTrackedDispatch<T>(
  plan: {
    eventId: string;
    resolveProject: () => ResolvedProject | Promise<ResolvedProject>;
    postCandidate: (project: ResolvedProject) => Promise<Admission>;
  },
  dependencies: {
    dispatch: DispatchFn<T>;
  },
): Promise<DispatchResult<T>> {
  let project: ResolvedProject;
  try {
    project = await plan.resolveProject();
  } catch (error) {
    const context = skippedContext(
      errorCode(error, "UNINITIALIZED_PROJECT"),
      plan.eventId,
    );

    const dispatchResult = await dependencies.dispatch(context);
    return { ...context, dispatchResult };
  }

  let admission: Admission;
  try {
    admission = await plan.postCandidate(project);
  } catch (error) {
    const context = skippedContext(errorCode(error, "ADAPTER_TRACKING_FAILURE"), plan.eventId);
    const dispatchResult = await dependencies.dispatch(context);
    return { ...context, dispatchResult };
  }
  const context = toDispatchContext(admission);

  const dispatchResult = await dependencies.dispatch(context);
  return { ...context, dispatchResult };
}
