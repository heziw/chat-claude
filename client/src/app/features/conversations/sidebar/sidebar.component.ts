import { Component, OnInit, computed, signal } from '@angular/core';
import { Router, RouterLink, RouterLinkActive } from '@angular/router';
import { AuthService } from '../../../core/services/auth.service';
import { ConversationsService } from '../../../core/services/conversations.service';
import { Conversation } from '../../../core/models';

@Component({
  selector: 'app-sidebar',
  standalone: true,
  imports: [RouterLink, RouterLinkActive],
  templateUrl: './sidebar.component.html',
  styleUrl: './sidebar.component.scss'
})
export class SidebarComponent implements OnInit {
  readonly conversations = signal<Conversation[]>([]);
  readonly isLoading = signal(false);
  readonly isCreating = signal(false);
  readonly errorMessage = signal<string | null>(null);

  readonly currentUserName = computed(() => this.auth.currentUser()?.display_name ?? '');

  readonly sortedConversations = computed(() =>
    [...this.conversations()].sort(
      (a, b) => new Date(b.last_active_at).getTime() - new Date(a.last_active_at).getTime()
    )
  );

  constructor(
    private readonly conversationsService: ConversationsService,
    private readonly auth: AuthService,
    private readonly router: Router
  ) {}

  ngOnInit(): void {
    this.loadConversations();
  }

  loadConversations(): void {
    this.isLoading.set(true);
    this.conversationsService.list().subscribe({
      next: (items) => {
        this.conversations.set(items);
        this.isLoading.set(false);
      },
      error: () => {
        this.errorMessage.set('טעינת רשימת השיחות נכשלה');
        this.isLoading.set(false);
      }
    });
  }

  createConversation(): void {
    this.isCreating.set(true);
    this.conversationsService.create().subscribe({
      next: (conversation) => {
        this.isCreating.set(false);
        this.conversations.update((list) => [conversation, ...list]);
        this.router.navigate(['/conversations', conversation.id]);
      },
      error: () => {
        this.isCreating.set(false);
        this.errorMessage.set('יצירת שיחה חדשה נכשלה');
      }
    });
  }

  logout(): void {
    this.auth.logout();
    this.router.navigate(['/login']);
  }

  formatLastActive(iso: string): string {
    try {
      return new Date(iso).toLocaleString('he-IL', {
        day: '2-digit',
        month: '2-digit',
        hour: '2-digit',
        minute: '2-digit'
      });
    } catch {
      return iso;
    }
  }
}
