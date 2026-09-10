import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, catchError, map, throwError } from 'rxjs';
import { CoreApiError } from '../../global/interfaces/api-error';
import {
  ResponseEnvelope,
  TaskData,
  TaskDetail,
  TaskSummary,
  TasksData,
} from '../interfaces/task';

export interface TaskListResult {
  tasks: TaskSummary[];
  nextCursor: string | null;
}

/**
 * Read-only Core `/v1` client. Uses relative URLs so the dev-server proxy
 * forwards to Core at `http://127.0.0.1:7331`. Cursors are opaque and are
 * passed through unchanged. The list endpoint never fetches per-row detail.
 */
@Injectable({ providedIn: 'root' })
export class TasksApi {
  private readonly http = inject(HttpClient);

  listTasks(limit = 50, cursor: string | null = null): Observable<TaskListResult> {
    let params = new HttpParams().set('limit', String(limit));
    if (cursor !== null && cursor !== '') {
      params = params.set('cursor', cursor);
    }
    return this.http
      .get<ResponseEnvelope<TasksData>>('/v1/tasks', { params })
      .pipe(
        map((envelope) => ({
          tasks: envelope.data.tasks,
          nextCursor: envelope.data.next_cursor,
        })),
        catchError((error: unknown) => throwError(() => toCoreError(error))),
      );
  }

  /**
   * Detail fetch. The initial call must pass `includeDiff=false`
   * (`?include_diff=false`); diff is fetched only on explicit user action
   * with `includeDiff=true` (the Core default).
   */
  getTask(taskId: string, includeDiff = false): Observable<TaskDetail> {
    const params = includeDiff
      ? undefined
      : new HttpParams().set('include_diff', 'false');
    return this.http
      .get<ResponseEnvelope<TaskData>>(`/v1/tasks/${taskId}`, { params })
      .pipe(
        map((envelope) => envelope.data.task),
        catchError((error: unknown) => throwError(() => toCoreError(error))),
      );
  }
}

/** Extract the safe Core `{ data: { code } }` error shape, if present. */
export function toCoreError(error: unknown): CoreApiError {
  if (isCoreApiError(error)) return error;
  if (error instanceof HttpErrorResponse) {
    const body = (error.error ?? null) as {
      message?: unknown;
      data?: { code?: unknown };
    } | null;
    const rawCode = body?.data?.code;
    const code =
      typeof rawCode === 'string' && rawCode.length > 0
        ? rawCode
        : httpFallbackCode(error.status);
    // Preserve the Core envelope message (sanitized); fall back to the
    // code only when no usable message is available.
    const message =
      sanitizeEnvelopeMessage(body?.message) ??
      (error.status === 0
        ? 'Core is unreachable at /v1. Start Core on 127.0.0.1:7331 and retry.'
        : `Core reported ${code}.`);
    return { code, message, status: error.status };
  }
  return { code: 'UNKNOWN_ERROR', message: 'An unexpected error occurred.' };
}

function isCoreApiError(value: unknown): value is CoreApiError {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { code?: unknown }).code === 'string'
  );
}

/**
 * Keep only a short, single-line, renderable message. Core envelope
 * messages are safe display strings, but control characters are blanked
 * and length is capped so error rendering stays predictable.
 */
function sanitizeEnvelopeMessage(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  let flattened = '';
  for (const ch of value) {
    const code = ch.codePointAt(0) ?? 32;
    flattened += code < 32 || code === 127 ? ' ' : ch;
  }
  const cleaned = flattened.replace(/\s+/g, ' ').trim();
  if (cleaned.length === 0) return null;
  return cleaned.length > 280 ? `${cleaned.slice(0, 279)}…` : cleaned;
}

function httpFallbackCode(status: number): string {
  if (status === 0) return 'CORE_UNREACHABLE';
  if (status === 404) return 'TASK_NOT_FOUND';
  return `HTTP_${status}`;
}
