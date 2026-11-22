import { createParamDecorator, ExecutionContext } from '@nestjs/common';
import { AuthenticatedUser } from './jwt-payload.interface';

/**
 * Pulls the authenticated user (attached by JwtAuthGuard) out of the request.
 * Usage: `@CurrentUser() user: AuthenticatedUser`.
 */
export const CurrentUser = createParamDecorator(
  (_data: unknown, ctx: ExecutionContext): AuthenticatedUser => {
    const request = ctx.switchToHttp().getRequest();
    return request.user;
  },
);
