import { HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { AuthService } from '../services/auth.service';

/** Functional interceptor: attaches `Authorization: Bearer <token>` to every
 * outgoing request when we have a token. */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(AuthService);
  const token = auth.token();

  if (!token) {
    return next(req);
  }

  const authorizedRequest = req.clone({
    setHeaders: { Authorization: `Bearer ${token}` }
  });

  return next(authorizedRequest);
};
