// Mirrors the `users` table from shared/CONTRACTS.md.
export interface AuthUser {
  id: string;
  display_name: string;
  role: string;
  sso_subject?: string;
}

// Response shape for POST /auth/dev-login. CONTRACTS.md does not pin down the
// exact field name, so we accept the common NestJS @nestjs/jwt convention
// (`access_token`) with a couple of fallbacks handled in AuthService.
export interface DevLoginResponse {
  access_token?: string;
  accessToken?: string;
  token?: string;
  user?: AuthUser;
}
