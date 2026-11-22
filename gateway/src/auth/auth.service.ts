import {
  ForbiddenException,
  Injectable,
  InternalServerErrorException,
  Logger,
  UnauthorizedException,
} from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { JwtService } from '@nestjs/jwt';
import { randomUUID } from 'crypto';
import { Issuer, generators, Client as OidcClient } from 'openid-client';
import { DatabaseService } from '../common/database/database.service';
import { UserEntity } from './user.entity';

export interface LoginResult {
  access_token: string;
  token_type: 'Bearer';
  user: {
    id: string;
    displayName: string;
    role: string;
  };
}

interface PendingAuthorization {
  state: string;
  nonce: string;
  codeVerifier: string;
  createdAt: number;
}

const PENDING_AUTHORIZATION_TTL_MS = 10 * 60 * 1000; // 10 minutes

@Injectable()
export class AuthService {
  private readonly logger = new Logger(AuthService.name);
  private readonly authMode: 'dev' | 'oidc';

  // In-memory PKCE/state store. Fine for a single gateway instance; if the
  // gateway is ever scaled horizontally this needs to move to Redis
  // (REDIS_URL is already available in the env for exactly this purpose).
  private readonly pendingAuthorizations = new Map<string, PendingAuthorization>();
  private oidcClientPromise: Promise<OidcClient> | null = null;

  constructor(
    private readonly config: ConfigService,
    private readonly jwtService: JwtService,
    private readonly db: DatabaseService,
  ) {
    this.authMode = this.config.get<string>('AUTH_MODE', 'dev') as 'dev' | 'oidc';
  }

  getAuthMode(): string {
    return this.authMode;
  }

  // ---------------------------------------------------------------------
  // dev mode
  // ---------------------------------------------------------------------

  async devLogin(username: string): Promise<LoginResult> {
    if (this.authMode !== 'dev') {
      throw new ForbiddenException(
        'POST /auth/dev-login is only available when AUTH_MODE=dev',
      );
    }
    if (!username || !username.trim()) {
      throw new UnauthorizedException('username is required');
    }

    const ssoSubject = `dev:${username.trim()}`;
    const user = await this.findOrCreateUser(ssoSubject, username.trim());
    return this.issueSession(user);
  }

  // ---------------------------------------------------------------------
  // oidc mode
  // ---------------------------------------------------------------------

  private async getOidcClient(): Promise<OidcClient> {
    if (this.authMode !== 'oidc') {
      throw new ForbiddenException('OIDC login is only available when AUTH_MODE=oidc');
    }
    if (!this.oidcClientPromise) {
      this.oidcClientPromise = this.buildOidcClient();
    }
    return this.oidcClientPromise;
  }

  private async buildOidcClient(): Promise<OidcClient> {
    const issuerUrl = this.config.get<string>('OIDC_ISSUER');
    const clientId = this.config.get<string>('OIDC_CLIENT_ID');
    const clientSecret = this.config.get<string>('OIDC_CLIENT_SECRET');
    const baseUrl = this.config.get<string>('GATEWAY_BASE_URL', 'http://localhost:3000');

    if (!issuerUrl || !clientId || !clientSecret) {
      throw new InternalServerErrorException(
        'OIDC_ISSUER, OIDC_CLIENT_ID and OIDC_CLIENT_SECRET must be set when AUTH_MODE=oidc',
      );
    }

    try {
      const issuer = await Issuer.discover(issuerUrl);
      return new issuer.Client({
        client_id: clientId,
        client_secret: clientSecret,
        redirect_uris: [`${baseUrl}/auth/callback`],
        response_types: ['code'],
      });
    } catch (err: any) {
      this.logger.error(`Failed to discover OIDC issuer ${issuerUrl}: ${err.message}`);
      throw new InternalServerErrorException('Failed to reach the OIDC identity provider');
    }
  }

  /** Builds the IdP authorization URL that /auth/login should redirect to. */
  async buildAuthorizationUrl(): Promise<string> {
    const client = await this.getOidcClient();

    const state = generators.state();
    const nonce = generators.nonce();
    const codeVerifier = generators.codeVerifier();
    const codeChallenge = generators.codeChallenge(codeVerifier);

    this.pendingAuthorizations.set(state, {
      state,
      nonce,
      codeVerifier,
      createdAt: Date.now(),
    });
    this.sweepExpiredAuthorizations();

    return client.authorizationUrl({
      scope: 'openid profile email',
      state,
      nonce,
      code_challenge: codeChallenge,
      code_challenge_method: 'S256',
    });
  }

  /** Handles the IdP redirect back to /auth/callback. */
  async handleCallback(query: Record<string, string>): Promise<LoginResult> {
    const client = await this.getOidcClient();
    const state = query.state;
    const pending = state ? this.pendingAuthorizations.get(state) : undefined;

    if (!pending) {
      throw new UnauthorizedException('Unknown or expired OIDC state');
    }
    this.pendingAuthorizations.delete(state);

    const baseUrl = this.config.get<string>('GATEWAY_BASE_URL', 'http://localhost:3000');

    let tokenSet;
    try {
      tokenSet = await client.callback(`${baseUrl}/auth/callback`, query, {
        state: pending.state,
        nonce: pending.nonce,
        code_verifier: pending.codeVerifier,
      });
    } catch (err: any) {
      this.logger.error(`OIDC callback exchange failed: ${err.message}`);
      throw new UnauthorizedException('OIDC code exchange failed');
    }

    if (!tokenSet.access_token) {
      throw new UnauthorizedException('IdP did not return an access token');
    }

    const claims = tokenSet.claims();
    let userinfo: Record<string, any> = claims;
    try {
      userinfo = { ...claims, ...(await client.userinfo(tokenSet.access_token)) };
    } catch (err: any) {
      // Non-fatal: some IdPs don't expose /userinfo for client_credentials-style
      // setups; the ID token claims are usually sufficient.
      this.logger.warn(`OIDC userinfo call failed, falling back to ID token claims: ${err.message}`);
    }

    const ssoSubject = String(userinfo.sub ?? claims.sub);
    const displayName =
      userinfo.name ?? userinfo.preferred_username ?? userinfo.email ?? ssoSubject;

    const user = await this.findOrCreateUser(ssoSubject, displayName);
    return this.issueSession(user);
  }

  private sweepExpiredAuthorizations(): void {
    const now = Date.now();
    for (const [state, entry] of this.pendingAuthorizations.entries()) {
      if (now - entry.createdAt > PENDING_AUTHORIZATION_TTL_MS) {
        this.pendingAuthorizations.delete(state);
      }
    }
  }

  // ---------------------------------------------------------------------
  // shared
  // ---------------------------------------------------------------------

  private async findOrCreateUser(ssoSubject: string, displayName: string): Promise<UserEntity> {
    const existing = await this.db.query<UserEntity>(
      'SELECT * FROM users WHERE sso_subject = $1',
      [ssoSubject],
    );
    if (existing.rows.length > 0) {
      return existing.rows[0];
    }

    const inserted = await this.db.query<UserEntity>(
      `INSERT INTO users (id, sso_subject, display_name, role)
       VALUES ($1, $2, $3, 'user')
       ON CONFLICT (sso_subject) DO UPDATE SET sso_subject = EXCLUDED.sso_subject
       RETURNING *`,
      [randomUUID(), ssoSubject, displayName],
    );
    return inserted.rows[0];
  }

  private issueSession(user: UserEntity): LoginResult {
    const access_token = this.jwtService.sign({
      sub: user.id,
      displayName: user.display_name,
      role: user.role,
    });
    return {
      access_token,
      token_type: 'Bearer',
      user: { id: user.id, displayName: user.display_name, role: user.role },
    };
  }
}
