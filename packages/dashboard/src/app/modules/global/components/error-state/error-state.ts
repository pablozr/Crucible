import { Component, input, output } from '@angular/core';
import { CoreApiError } from '../../interfaces/api-error';

@Component({
  selector: 'cru-error-state',
  standalone: true,
  templateUrl: './error-state.html',
})
export class ErrorState {
  readonly error = input.required<CoreApiError>();
  readonly retry = output<void>();
}
