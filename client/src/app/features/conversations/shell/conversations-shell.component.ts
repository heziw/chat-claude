import { Component } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { SidebarComponent } from '../sidebar/sidebar.component';

/** Layout shell for /conversations and /conversations/:id — sidebar on one
 *  side, the active route (empty state or chat) in the outlet. */
@Component({
  selector: 'app-conversations-shell',
  standalone: true,
  imports: [RouterOutlet, SidebarComponent],
  templateUrl: './conversations-shell.component.html',
  styleUrl: './conversations-shell.component.scss'
})
export class ConversationsShellComponent {}
