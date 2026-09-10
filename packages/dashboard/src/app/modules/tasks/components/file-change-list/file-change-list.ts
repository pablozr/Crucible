import { Component, input } from '@angular/core';
import { TaskFileChange } from '../../interfaces/task';

/** Changed files with evidence status and reason. Text only. */
@Component({
  selector: 'cru-file-change-list',
  standalone: true,
  templateUrl: './file-change-list.html',
  styleUrl: './file-change-list.css',
})
export class FileChangeList {
  readonly changes = input.required<TaskFileChange[]>();
}
