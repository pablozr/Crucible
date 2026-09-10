import { Component } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { AppShell } from './modules/global/components/app-shell/app-shell';

@Component({
  selector: 'cru-root',
  standalone: true,
  imports: [RouterOutlet, AppShell],
  template: `<cru-app-shell><router-outlet /></cru-app-shell>`,
})
export class App {}
