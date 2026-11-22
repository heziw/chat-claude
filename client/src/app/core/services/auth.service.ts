import { HttpClient } from '@angular/common/http';
import { Injectable, computed, signal } from '@angular/core';
import { Observable, tap } from 'rxjs';
import { environment } from '../../../environments/environment';
import { AuthUser, DevLoginResponse } from '../models';

const TOKEN_STORAGE_KEY = 'rag_chat_token';
const USER_STORAGE_KEY = 'rag_chat_user';

/**
 * Auth state + login flows.
 *
 * Dev mode (current, implemented below): POST /auth/dev-login with just a
 * username, no real IdP involved. Only enabled on the gateway when
 * AUTH_MODE=dev (see shared/CONTRACTS.md).
 *
 * TODO(oidc): Production/OIDC redirect flow goes here. It should:
 *   1. Navigate the browser to `GET {apiBaseUrl}/auth/login` (gateway kicks
 *      off the OIDC redirect to the IdP).
 *   2. The IdP redirects back to the gateway's `/auth/callback`.
 *   3. The gateway redirects back to the Angular app (e.g. `/auth/complete`)
 *      with a token (query param or httpOnly cookie) that this service picks
 *      up and stores the same way `devLogin()` does below.
 * Whoever implements this should add an `AUTH_MODE` check (dev | oidc) in the
 * login screen to show the right UI, and a route/component for step 3.
 */
@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly tokenSignal = signal<string | null>(this.readStoredToken());
  private readonly userSignal = signal<AuthUser | null>(this.readStoredUser());

  readonly token = computed(() => this.tokenSignal());
  readonly currentUser = computed(() => this.userSignal());
  readonly isAuthenticated = computed(() => !!this.tokenSignal());

  constructor(private readonly http: HttpClient) {}

  /** Dev-mode login: username only, no password/IdP. */
  devLogin(username: string): Observable<DevLoginResponse> {
    return this.http
      .post<DevLoginResponse>(`${environment.apiBaseUrl}/auth/dev-login`, { username })
      .pipe(
        tap((response) => {
          const token = response.access_token ?? response.accessToken ?? response.token ?? null;
          if (!token) {
            throw new Error('תגובת ה-login לא כללה טוקן');
          }
          const user = response.user ?? this.decodeUserFromToken(token, username);
          this.setSession(token, user);
        })
      );
  }

  logout(): void {
    this.tokenSignal.set(null);
    this.userSignal.set(null);
    localStorage.removeItem(TOKEN_STORAGE_KEY);
    localStorage.removeItem(USER_STORAGE_KEY);
  }

  private setSession(token: string, user: AuthUser | null): void {
    this.tokenSignal.set(token);
    this.userSignal.set(user);
    localStorage.setItem(TOKEN_STORAGE_KEY, token);
    if (user) {
      localStorage.setItem(USER_STORAGE_KEY, JSON.stringify(user));
    }
  }

  private readStoredToken(): string | null {
    return localStorage.getItem(TOKEN_STORAGE_KEY);
  }

  private readStoredUser(): AuthUser | null {
    const raw = localStorage.getItem(USER_STORAGE_KEY);
    if (!raw) return null;
    try {
      return JSON.parse(raw) as AuthUser;
    } catch {
      return null;
    }
  }

  /** Best-effort fallback: decode the JWT payload to recover user info if the
   *  dev-login response doesn't include a `user` object directly. */
  private decodeUserFromToken(token: string, fallbackUsername: string): AuthUser | null {
    try {
      const payloadSegment = token.split('.')[1];
      if (!payloadSegment) return null;
      const json = atob(payloadSegment.replace(/-/g, '+').replace(/_/g, '/'));
      const payload = JSON.parse(json) as Record<string, unknown>;
      return {
        id: String(payload['sub'] ?? payload['id'] ?? fallbackUsername),
        display_name: String(payload['display_name'] ?? payload['name'] ?? fallbackUsername),
        role: String(payload['role'] ?? 'user')
      };
    } catch {
      return { id: fallbackUsername, display_name: fallbackUsername, role: 'user' };
    }
  }
}
