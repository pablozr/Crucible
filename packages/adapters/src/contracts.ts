export type Delivery = "new" | "steer";

export type TrackedDispatchContext = {
  tracked: true;
  outcome: "admitted";
  taskId: string | null;
  inputId: string | null;
  eventId: string;
};

export type UntrackedDispatchContext = {
  tracked: false;
  outcome: string;
  diagnostic: string;
  taskId: string | null;
  inputId: string | null;
  eventId: string;
};

export type DispatchContext =
  | TrackedDispatchContext
  | UntrackedDispatchContext;

export type DispatchFn<T> = (context: DispatchContext) => Promise<T> | T;

export type DispatchResult<T> = DispatchContext & {
  dispatchResult: T;
};

export type DispatchDependencies<T> = {
  dispatch: DispatchFn<T>;
};
