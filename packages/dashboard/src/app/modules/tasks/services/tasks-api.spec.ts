import { HttpErrorResponse } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { TasksApi, toCoreError } from './tasks-api';
import { ResponseEnvelope, TaskData, TaskDetail, TasksData } from '../interfaces/task';

function summary(id: string) {
  return {
    id,
    status: 'completed',
    started_at: '2026-09-10T00:00:00Z',
    worktree: '/tmp/wt',
    project_id: 'proj-1',
    branch: null,
    failure_code: null,
    failure_message: null,
  };
}

function detail(id: string, taskDiff: string | null): TaskDetail {
  return {
    ...summary(id),
    baseline_head: null,
    baseline_status: null,
    baseline_index_manifest: null,
    baseline_index_sha256: null,
    input_ids: ['in-1'],
    baseline_files: [],
    final_head: null,
    final_branch: null,
    final_status: null,
    final_index_manifest: null,
    final_index_sha256: null,
    snapshot_frozen_at: null,
    task_diff: taskDiff,
    evidence_completeness: null,
    execution_id: null,
    terminal_signal: null,
    terminal_outcome: null,
    compatibility_profile: null,
    terminal_observed_at: null,
    capture_not_after: null,
    file_changes: [],
  };
}

describe('TasksApi', () => {
  let api: TasksApi;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), TasksApi],
    });
    api = TestBed.inject(TasksApi);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
  });

  it('lists tasks without a cursor by default and passes cursors through opaque', () => {
    const envelope: ResponseEnvelope<TasksData> = {
      status: 'ok',
      message: 'Tasks retrieved.',
      data: { tasks: [summary('t-1')], next_cursor: 'opaque-cursor-1' },
    };
    api.listTasks().subscribe((result) => {
      expect(result.tasks).toHaveLength(1);
      expect(result.nextCursor).toBe('opaque-cursor-1');
    });
    const first = http.expectOne('/v1/tasks?limit=50');
    expect(first.request.method).toBe('GET');
    first.flush(envelope);

    api.listTasks(50, 'opaque-cursor-1').subscribe((result) => {
      expect(result.nextCursor).toBeNull();
    });
    const second = http.expectOne(
      (req) => req.url === '/v1/tasks' && req.params.get('cursor') === 'opaque-cursor-1',
    );
    expect(second.request.method).toBe('GET');
    second.flush({
      status: 'ok',
      message: 'Tasks retrieved.',
      data: { tasks: [summary('t-2')], next_cursor: null },
    } satisfies ResponseEnvelope<TasksData>);
  });

  it('requests detail with include_diff=false by default', () => {
    api.getTask('task-1', false).subscribe((task) => {
      expect(task.id).toBe('task-1');
      expect(task.task_diff).toBeNull();
    });
    const req = http.expectOne(
      (r) => r.url === '/v1/tasks/task-1' && r.params.get('include_diff') === 'false',
    );
    expect(req.request.method).toBe('GET');
    req.flush({
      status: 'ok',
      message: 'Task retrieved.',
      data: { task: detail('task-1', null) },
    } satisfies ResponseEnvelope<TaskData>);
  });

  it('requests the default detail URL when loading the diff', () => {
    api.getTask('task-1', true).subscribe((task) => {
      expect(task.task_diff).toBe('diff --git a/x b/x');
    });
    const req = http.expectOne('/v1/tasks/task-1');
    expect(req.request.method).toBe('GET');
    expect(req.request.params.has('include_diff')).toBe(false);
    req.flush({
      status: 'ok',
      message: 'Task retrieved.',
      data: { task: detail('task-1', 'diff --git a/x b/x') },
    } satisfies ResponseEnvelope<TaskData>);
  });

  it('surfaces the Core error code and message from error envelopes', () => {
    api.getTask('missing', false).subscribe({
      next: () => expect.unreachable(),
      error: (error: { code: string; message: string }) => {
        expect(error.code).toBe('TASK_NOT_FOUND');
        expect(error.message).toBe('Task missing from the local store.');
      },
    });
    const req = http.expectOne((r) => r.url === '/v1/tasks/missing');
    req.flush(
      {
        status: 'error',
        message: 'Task missing from the local store.',
        data: { code: 'TASK_NOT_FOUND' },
      },
      { status: 404, statusText: 'Not Found' },
    );
  });

  it('falls back to the code alone when the envelope has no message', () => {
    api.listTasks().subscribe({
      next: () => expect.unreachable(),
      error: (error: { code: string; message: string }) => {
        expect(error.code).toBe('INVALID_CURSOR');
        expect(error.message).toBe('Core reported INVALID_CURSOR.');
      },
    });
    http
      .expectOne('/v1/tasks?limit=50')
      .flush(
        { status: 'error', message: '', data: { code: 'INVALID_CURSOR' } },
        { status: 400, statusText: 'Bad Request' },
      );
  });

  it('maps unreachable Core to CORE_UNREACHABLE', () => {
    const error = toCoreError(
      new HttpErrorResponse({ error: null, status: 0, statusText: 'Unknown Error' }),
    );
    expect(error.code).toBe('CORE_UNREACHABLE');
  });
});
