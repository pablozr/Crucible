import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { TaskDetailPage } from './task-detail-page';
import { TaskDetail } from '../../interfaces/task';

function detailEnvelope(taskDiff: string | null) {
  const task: TaskDetail = {
    id: 'task-1',
    status: 'failed',
    started_at: '2026-09-10T00:00:00Z',
    worktree: '/tmp/wt',
    project_id: 'proj-1',
    branch: 'agent/task-1',
    failure_code: 'TERMINAL_NONZERO',
    failure_message: 'exit 1',
    baseline_head: 'abc123',
    baseline_status: 'captured',
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
    evidence_completeness: 'partial',
    execution_id: 'exec-1',
    terminal_signal: null,
    terminal_outcome: 'nonzero',
    compatibility_profile: null,
    terminal_observed_at: null,
    capture_not_after: null,
    file_changes: [],
  };
  return { status: 'ok', message: 'Task retrieved.', data: { task } };
}

describe('TaskDetailPage', () => {
  let http: HttpTestingController;
  let fixture: ComponentFixture<TaskDetailPage>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [TaskDetailPage],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
      ],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
  });

  function create(): ComponentFixture<TaskDetailPage> {
    fixture = TestBed.createComponent(TaskDetailPage);
    fixture.componentRef.setInput('taskId', 'task-1');
    fixture.detectChanges();
    return fixture;
  }

  it('loads detail with include_diff=false and keeps the diff closed', () => {
    create();
    const req = http.expectOne(
      (r) => r.url === '/v1/tasks/task-1' && r.params.get('include_diff') === 'false',
    );
    req.flush(detailEnvelope(null));
    fixture.detectChanges();

    expect(fixture.componentInstance.task()?.id).toBe('task-1');
    expect(fixture.componentInstance.diff()).toEqual({ kind: 'closed' });
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('TERMINAL_NONZERO');
    expect(text).toContain('Load diff');
  });

  it('loads the diff only after the explicit action', () => {
    create();
    http
      .expectOne((r) => r.url === '/v1/tasks/task-1')
      .flush(detailEnvelope(null));
    fixture.detectChanges();

    fixture.componentInstance.loadDiff();
    const diffReq = http.expectOne('/v1/tasks/task-1');
    expect(diffReq.request.params.has('include_diff')).toBe(false);
    diffReq.flush(detailEnvelope('diff --git a/x b/x'));
    fixture.detectChanges();

    expect(fixture.componentInstance.diff()).toEqual({
      kind: 'ready',
      diff: 'diff --git a/x b/x',
    });
    const pre = (fixture.nativeElement as HTMLElement).querySelector('pre.cru-diff');
    expect(pre?.textContent).toContain('diff --git');
  });

  it('renders task-not-found as an empty state', () => {
    create();
    http
      .expectOne((r) => r.url === '/v1/tasks/task-1')
      .flush(
        { status: 'error', message: 'Missing.', data: { code: 'TASK_NOT_FOUND' } },
        { status: 404, statusText: 'Not Found' },
      );
    fixture.detectChanges();

    expect(fixture.componentInstance.isNotFound()).toBe(true);
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Task not found');
  });
});
