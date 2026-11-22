/** Shape of the JWT payload issued by AuthService and verified by JwtAuthGuard. */
export interface JwtPayload {
  /** users.id (UUID) */
  sub: string;
  /** users.display_name */
  displayName: string;
  /** users.role */
  role: string;
  iat?: number;
  exp?: number;
}

/** `request.user` as attached by JwtAuthGuard. */
export type AuthenticatedUser = JwtPayload;
