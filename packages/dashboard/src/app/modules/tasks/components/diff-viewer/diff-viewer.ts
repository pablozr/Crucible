import { Component, input, output } from '@angular/core';
import { CoreApiError } from '../../../global/interfaces/api-error';

export type DiffState =
  | { kind: 'closed' }
  | { kind: 'loading' }
  | { kind: 'ready'; diff: string | null }
  | { kind: 'error'; error: CoreApiError };

/**
 * On-demand Task Diff. Text-only rendering inside an accessible
 * scrollable `<pre>` — never HTML.
 */
@Component({
  selector: 'cru-diff-viewer',
  standalone: true,
  templateUrl: './diff-viewer.html',
})
export class DiffViewer {
  readonly state = input.required<DiffState>();
  readonly load = output<void>();
  readonly retry = output<void>();
}
