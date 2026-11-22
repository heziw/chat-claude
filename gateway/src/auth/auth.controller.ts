import { Body, Controller, Get, Post, Query, Res } from '@nestjs/common';
import { Response } from 'express';
import { AuthService, LoginResult } from './auth.service';
import { DevLoginDto } from './dto/dev-login.dto';

@Controller('auth')
export class AuthController {
  constructor(private readonly authService: AuthService) {}

  /** dev-mode only: trade a bare username for a JWT session, no IdP involved. */
  @Post('dev-login')
  async devLogin(@Body() body: DevLoginDto): Promise<LoginResult> {
    return this.authService.devLogin(body.username);
  }

  /** oidc-mode only: redirects the browser to the IdP's authorization endpoint. */
  @Get('login')
  async login(@Res() res: Response): Promise<void> {
    const url = await this.authService.buildAuthorizationUrl();
    res.redirect(url);
  }

  /** oidc-mode only: the IdP's redirect_uri. Exchanges `code` for tokens and
   * returns a gateway-issued JWT session, same shape as /auth/dev-login. */
  @Get('callback')
  async callback(@Query() query: Record<string, string>): Promise<LoginResult> {
    return this.authService.handleCallback(query);
  }
}
