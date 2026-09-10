import { Routes } from '@angular/router';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'tasks' },
  {
    path: 'tasks',
    loadComponent: () =>
      import('./modules/tasks/pages/task-list-page/task-list-page').then(
        (m) => m.TaskListPage,
      ),
  },
  {
    path: 'tasks/:taskId',
    loadComponent: () =>
      import('./modules/tasks/pages/task-detail-page/task-detail-page').then(
        (m) => m.TaskDetailPage,
      ),
  },
  {
    path: '**',
    loadComponent: () =>
      import('./modules/global/components/not-found/not-found').then(
        (m) => m.NotFound,
      ),
  },
];
