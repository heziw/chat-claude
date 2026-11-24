import { Component, ChangeDetectionStrategy } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { SidebarComponent } from '../sidebar/sidebar.component';

/** Layout shell for /conversations and /conversations/:id — sidebar on one
 *  side, the active route (empty state or chat) in the outlet. */
@Component({
    selector: 'app-conversations-shell',
    imports: [RouterOutlet, SidebarComponent],
    templateUrl: './conversations-shell.component.html',
    changeDetection: ChangeDetectionStrategy.Eager,
    styleUrl: './conversations-shell.component.scss'
})
export class ConversationsShellComponent {}
