import { Component, OnInit, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { CoreApiError } from '../../../global/interfaces/api-error';
import { EmptyState } from '../../../global/components/empty-state/empty-state';
import { ErrorState } from '../../../global/components/error-state/error-state';
import { TaskDetail } from '../../interfaces/task';
import { TasksApi, toCoreError } from '../../services/tasks-api';
import { TaskStatusBadge } from '../../components/task-status-badge/task-status-badge';
import {
  DiffState,
  DiffViewer,
} from '../../components/diff-viewer/diff-viewer';
import { EvidenceSummary } from '../../components/evidence-summary/evidence-summary';
import { FileChangeList } from '../../components/file-change-list/file-change-list';

/**
 * Task detail orchestrator. The initial fetch uses `include_diff=false`;
 * the stored diff is fetched only when the user selects **Load diff**.
 */
@Component({
  selector: 'cru-task-detail-page',
  standalone: true,
  imports: [
    RouterLink,
    TaskStatusBadge,
    EvidenceSummary,
    FileChangeList,
    DiffViewer,
    ErrorState,
    EmptyState,
  ],
  templateUrl: './task-detail-page.html',
  styleUrl: './task-detail-page.css',
})
export class TaskDetailPage implements OnInit {
  /** Bound from the `tasks/:taskId` route. */
  readonly taskId = input.required<string>();

  private readonly api = inject(TasksApi);

  readonly task = signal<TaskDetail | null>(null);
  readonly loading = signal(true);
  readonly error = signal<CoreApiError | null>(null);
  readonly diff = signal<DiffState>({ kind: 'closed' });

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    const id = this.taskId();
    this.loading.set(true);
    this.error.set(null);
    this.api.getTask(id, false).subscribe({
      next: (detail) => {
        this.task.set(detail);
        this.loading.set(false);
      },
      error: (cause: unknown) => {
        this.error.set(toCoreError(cause));
        this.loading.set(false);
      },
    });
  }

  loadDiff(): void {
    const current = this.task();
    if (current === null || this.isDiffLoading()) return;
    this.diff.set({ kind: 'loading' });
    this.api.getTask(current.id, true).subscribe({
      next: (detail) => {
        this.task.set(detail);
        this.diff.set({ kind: 'ready', diff: detail.task_diff });
      },
      error: (cause: unknown) => {
        this.diff.set({ kind: 'error', error: toCoreError(cause) });
      },
    });
  }

  private isDiffLoading(): boolean {
    return this.diff().kind === 'loading';
  }

  isNotFound(): boolean {
    return this.error()?.code === 'TASK_NOT_FOUND';
  }
}
