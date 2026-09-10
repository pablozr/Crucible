/** Safe, renderable subset of a Core error envelope. */
export interface CoreApiError {
  /** Opaque Core error code, e.g. TASK_NOT_FOUND. */
  code: string;
  /** Safe display message (Core message or HTTP fallback). Never a secret. */
  message: string;
  /** HTTP status when known. */
  status?: number;
}
