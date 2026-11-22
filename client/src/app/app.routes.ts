import { Routes } from '@angular/router';
import { authGuard } from './core/guards/auth.guard';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'conversations' },
  {
    path: 'login',
    loadComponent: () =>
      import('./features/login/login.component').then((m) => m.LoginComponent)
  },
  {
    path: 'conversations',
    canActivate: [authGuard],
    loadComponent: () =>
      import('./features/conversations/shell/conversations-shell.component').then(
        (m) => m.ConversationsShellComponent
      ),
    children: [
      {
        path: '',
        pathMatch: 'full',
        loadComponent: () =>
          import('./features/conversations/empty-state/empty-state.component').then(
            (m) => m.EmptyStateComponent
          )
      },
      {
        path: ':id',
        loadComponent: () =>
          import('./features/conversations/chat/chat.component').then((m) => m.ChatComponent)
      }
    ]
  },
  { path: '**', redirectTo: 'conversations' }
];
