import { Component, input } from '@angular/core';
import { TaskDetail } from '../../interfaces/task';

/** Admitted inputs, baseline/final metadata and evidence completeness. */
@Component({
  selector: 'cru-evidence-summary',
  standalone: true,
  templateUrl: './evidence-summary.html',
  styleUrl: './evidence-summary.css',
})
export class EvidenceSummary {
  readonly task = input.required<TaskDetail>();
}
