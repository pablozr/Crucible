import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { TaskListPage } from './task-list-page';

function listEnvelope(ids: string[], nextCursor: string | null) {
  return {
    status: 'ok',
    message: 'Tasks retrieved.',
    data: {
      tasks: ids.map((id) => ({
        id,
        status: 'completed',
        started_at: '2026-09-10T00:00:00Z',
        worktree: '/tmp/wt',
        project_id: 'proj-1',
        branch: null,
        failure_code: null,
        failure_message: null,
      })),
      next_cursor: nextCursor,
    },
  };
}

describe('TaskListPage', () => {
  let http: HttpTestingController;
  let fixture: ComponentFixture<TaskListPage>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [TaskListPage],
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

  function create(): ComponentFixture<TaskListPage> {
    fixture = TestBed.createComponent(TaskListPage);
    fixture.detectChanges();
    return fixture;
  }

  it('appends the next page without losing prior rows', () => {
    create();
    http.expectOne('/v1/tasks?limit=50').flush(listEnvelope(['t-1'], 'cursor-1'));
    fixture.detectChanges();
    expect(fixture.componentInstance.tasks()).toHaveLength(1);

    fixture.componentInstance.loadMore();
    const second = http.expectOne(
      (req) => req.url === '/v1/tasks' && req.params.get('cursor') === 'cursor-1',
    );
    second.flush(listEnvelope(['t-2'], null));
    fixture.detectChanges();

    expect(fixture.componentInstance.tasks().map((t) => t.id)).toEqual(['t-1', 't-2']);
    expect(fixture.componentInstance.nextCursor()).toBeNull();
  });

  it('shows retry when the list request fails', () => {
    create();
    http
      .expectOne('/v1/tasks?limit=50')
      .flush(
        { status: 'error', message: 'Bad cursor.', data: { code: 'INVALID_CURSOR' } },
        { status: 400, statusText: 'Bad Request' },
      );
    fixture.detectChanges();

    expect(fixture.componentInstance.error()?.code).toBe('INVALID_CURSOR');
    expect(fixture.componentInstance.error()?.message).toBe('Bad cursor.');
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('INVALID_CURSOR');
    expect(text).toContain('Bad cursor.');
    const retry = (fixture.nativeElement as HTMLElement).querySelector('button');
    expect(retry?.textContent).toContain('Retry');
  });
});
