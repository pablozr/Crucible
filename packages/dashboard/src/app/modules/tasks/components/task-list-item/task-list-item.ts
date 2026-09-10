import { Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';
import { TaskSummary } from '../../interfaces/task';
import { TaskStatusBadge } from '../task-status-badge/task-status-badge';

@Component({
  selector: 'cru-task-list-item',
  standalone: true,
  imports: [RouterLink, TaskStatusBadge],
  templateUrl: './task-list-item.html',
  styleUrl: './task-list-item.css',
})
export class TaskListItem {
  readonly task = input.required<TaskSummary>();
}
