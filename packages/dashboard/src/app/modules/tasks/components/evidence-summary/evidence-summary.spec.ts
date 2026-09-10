import { ComponentFixture, TestBed } from '@angular/core/testing';
import { EvidenceSummary } from './evidence-summary';
import { TaskDetail } from '../../interfaces/task';

const SECRET_CONTENT = 'c2VjcmV0LWJhc2U2NC1jb250ZW50LW5ldmVyLXJlbmRlcmVk';

function task(): TaskDetail {
  return {
    id: 'task-1',
    status: 'completed',
    started_at: '2026-09-10T00:00:00Z',
    worktree: '/tmp/wt',
    project_id: 'proj-1',
    branch: null,
    failure_code: null,
    failure_message: null,
    baseline_head: 'abc123',
    baseline_status: 'captured',
    baseline_index_manifest: '{"files":["src/a.ts"]}',
    baseline_index_sha256: 'base-sha',
    input_ids: ['in-1'],
    baseline_files: [
      {
        path: 'src/a.ts',
        status: 'captured',
        sha256: 'file-sha',
        size: 128,
        is_binary: false,
        content: SECRET_CONTENT,
        mode: '100644',
        gitlink_oid: null,
      },
    ],
    final_head: null,
    final_branch: null,
    final_status: null,
    final_index_manifest: null,
    final_index_sha256: 'final-sha',
    snapshot_frozen_at: '2026-09-10T00:01:00Z',
    task_diff: null,
    evidence_completeness: 'complete',
    execution_id: null,
    terminal_signal: null,
    terminal_outcome: null,
    compatibility_profile: null,
    terminal_observed_at: null,
    capture_not_after: null,
    file_changes: [],
  };
}

describe('EvidenceSummary', () => {
  let fixture: ComponentFixture<EvidenceSummary>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [EvidenceSummary],
    }).compileComponents();
  });

  function create(detail: TaskDetail): HTMLElement {
    fixture = TestBed.createComponent(EvidenceSummary);
    fixture.componentRef.setInput('task', detail);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('shows index manifest SHAs, snapshot time and collapsible manifest blobs', () => {
    const el = create(task());
    const text = el.textContent ?? '';
    expect(text).toContain('base-sha');
    expect(text).toContain('final-sha');
    expect(text).toContain('2026-09-10T00:01:00Z');

    const blobs = el.querySelectorAll('pre.cru-diff');
    expect(blobs).toHaveLength(1);
    expect(blobs[0].textContent).toContain('{"files":["src/a.ts"]}');
    // The missing final manifest renders as Unavailable, never an empty blob.
    expect(text).toContain('Final index manifest');
  });

  it('lists baseline files with metadata and never renders file content', () => {
    const el = create(task());
    const text = el.textContent ?? '';
    expect(text).toContain('Baseline files (1)');
    expect(text).toContain('src/a.ts');
    expect(text).toContain('captured');
    expect(text).toContain('128');
    expect(text).toContain('file-sha');
    expect(text).not.toContain(SECRET_CONTENT);
  });

  it('renders nulls as Not recorded or Unavailable', () => {
    const empty: TaskDetail = {
      ...task(),
      snapshot_frozen_at: null,
      baseline_index_manifest: null,
      baseline_index_sha256: null,
      final_index_manifest: null,
      final_index_sha256: null,
      baseline_files: [],
      input_ids: [],
    };
    const el = create(empty);
    const text = el.textContent ?? '';
    expect(text).toContain('Baseline files (0)');
    expect(text).toContain('Not recorded');
    expect(text).toContain('Unavailable');
    expect(el.querySelector('pre.cru-diff')).toBeNull();
  });
});
