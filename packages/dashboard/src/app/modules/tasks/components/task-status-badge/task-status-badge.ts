import { Component, computed, input } from '@angular/core';

type Tone = 'ok' | 'bad' | 'warn' | 'info';

/** Semantic status badge. Status colors communicate state, never decoration. */
@Component({
  selector: 'cru-task-status-badge',
  standalone: true,
  template: `<span class="cru-badge" [attr.data-tone]="tone()">{{ status() }}</span>`,
})
export class TaskStatusBadge {
  readonly status = input.required<string>();

  readonly tone = computed<Tone>(() => {
    const value = this.status().toLowerCase();
    if (value === 'completed') return 'ok';
    if (value === 'failed') return 'bad';
    if (value === 'running' || value === 'finalizing') return 'warn';
    return 'info';
  });
}
