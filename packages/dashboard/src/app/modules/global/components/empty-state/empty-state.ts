import { Component, input } from '@angular/core';

@Component({
  selector: 'cru-empty-state',
  standalone: true,
  templateUrl: './empty-state.html',
})
export class EmptyState {
  readonly title = input.required<string>();
  readonly hint = input<string>('');
}
