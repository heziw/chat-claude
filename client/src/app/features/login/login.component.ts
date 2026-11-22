import { Component, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { AuthService } from '../../core/services/auth.service';

/**
 * Dev-mode login screen: username only, calls POST /auth/dev-login.
 *
 * TODO(oidc): when AUTH_MODE=oidc on the gateway, this screen should instead
 * show a single "התחברות באמצעות SSO" button that navigates the browser to
 * `{apiBaseUrl}/auth/login` and lets the OIDC redirect flow take over (see
 * the TODO in AuthService for the full flow). For now this form is the only
 * supported path.
 */
@Component({
  selector: 'app-login',
  standalone: true,
  imports: [FormsModule],
  templateUrl: './login.component.html',
  styleUrl: './login.component.scss'
})
export class LoginComponent {
  readonly username = signal('');
  readonly isSubmitting = signal(false);
  readonly errorMessage = signal<string | null>(null);

  constructor(
    private readonly auth: AuthService,
    private readonly router: Router
  ) {}

  submit(): void {
    const username = this.username().trim();
    if (!username) {
      this.errorMessage.set('יש להזין שם משתמש');
      return;
    }

    this.isSubmitting.set(true);
    this.errorMessage.set(null);

    this.auth.devLogin(username).subscribe({
      next: () => {
        this.isSubmitting.set(false);
        this.router.navigate(['/conversations']);
      },
      error: (err) => {
        this.isSubmitting.set(false);
        this.errorMessage.set(
          err?.error?.message ?? 'ההתחברות נכשלה. בדוק שה-gateway פועל במצב dev.'
        );
      }
    });
  }
}
