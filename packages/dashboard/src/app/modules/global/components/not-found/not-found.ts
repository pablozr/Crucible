import { Component } from '@angular/core';
import { RouterLink } from '@angular/router';

@Component({
  selector: 'cru-not-found',
  standalone: true,
  imports: [RouterLink],
  templateUrl: './not-found.html',
})
export class NotFound {}
