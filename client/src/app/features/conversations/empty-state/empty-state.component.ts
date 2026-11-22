import { Component } from '@angular/core';

@Component({
  selector: 'app-empty-state',
  standalone: true,
  template: `
    <div class="empty-state">
      <p>בחר שיחה קיימת מהרשימה, או צור שיחה חדשה כדי להתחיל.</p>
    </div>
  `,
  styles: [
    `
      .empty-state {
        flex: 1;
        display: flex;
        align-items: center;
        justify-content: center;
        color: var(--color-text-muted);
        font-size: 1rem;
        text-align: center;
        padding: 2rem;
      }
    `
  ]
})
export class EmptyStateComponent {}
