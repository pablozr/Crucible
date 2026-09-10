import { Component, inject, signal } from '@angular/core';
import { CoreApiError } from '../../../global/interfaces/api-error';
import { EmptyState } from '../../../global/components/empty-state/empty-state';
import { ErrorState } from '../../../global/components/error-state/error-state';
import { TaskSummary } from '../../interfaces/task';
import { TasksApi, toCoreError } from '../../services/tasks-api';
import { TaskListItem } from '../../components/task-list-item/task-list-item';

const PAGE_SIZE = 50;

/** Cursor-paginated Task list. Never fetches per-row detail. */
@Component({
  selector: 'cru-task-list-page',
  standalone: true,
  imports: [TaskListItem, ErrorState, EmptyState],
  templateUrl: './task-list-page.html',
  styleUrl: './task-list-page.css',
})
export class TaskListPage {
  private readonly api = inject(TasksApi);

  readonly tasks = signal<TaskSummary[]>([]);
  readonly nextCursor = signal<string | null>(null);
  readonly loading = signal(true);
  readonly loadingMore = signal(false);
  readonly error = signal<CoreApiError | null>(null);

  constructor() {
    this.refresh();
  }

  refresh(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.listTasks(PAGE_SIZE).subscribe({
      next: (result) => {
        this.tasks.set(result.tasks);
        this.nextCursor.set(result.nextCursor);
        this.loading.set(false);
      },
      error: (cause: unknown) => {
        this.error.set(toCoreError(cause));
        this.loading.set(false);
      },
    });
  }

  loadMore(): void {
    const cursor = this.nextCursor();
    if (cursor === null || this.loadingMore()) return;
    this.loadingMore.set(true);
    this.error.set(null);
    this.api.listTasks(PAGE_SIZE, cursor).subscribe({
      next: (result) => {
        // Append without losing prior rows; cursor passes through opaque.
        this.tasks.update((current) => [...current, ...result.tasks]);
        this.nextCursor.set(result.nextCursor);
        this.loadingMore.set(false);
      },
      error: (cause: unknown) => {
        this.error.set(toCoreError(cause));
        this.loadingMore.set(false);
      },
    });
  }
}
