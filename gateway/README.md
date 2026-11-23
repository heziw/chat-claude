# gateway

NestJS BFF that sits between the Angular client and the backend services (see
`/shared/CONTRACTS.md` for the full system contract). Listens on `:3000`.

## Why raw `mssql`, not TypeORM/Prisma

The MSSQL schema is owned by `shared/CONTRACTS.md` and already created by
`infra/docker/mssql-init.sql` — the gateway never migrates it. An ORM's
main value (schema management, migrations) isn't needed here, and mapping a
handful of known tables/queries with parameterized SQL (`src/common/database`)
keeps every query auditable against the contract instead of hidden behind
entity decorators. If the schema grows substantially this can be revisited.

`DatabaseService.query(text, params)` keeps the `pg`-style `$1, $2, ...`
placeholder + `{ rows }` return shape the rest of the gateway was written
against; internally it rewrites placeholders to mssql's `@p1, @p2, ...` and
binds them via `Request#input`. IDs are generated in the app
(`crypto.randomUUID()`) before INSERT and `OUTPUT INSERTED.*` is used in
place of Postgres's `RETURNING *` (see shared/CONTRACTS.md).

## Modules

- `auth/` — `POST /auth/dev-login` (AUTH_MODE=dev) and `GET /auth/login` +
  `GET /auth/callback` (AUTH_MODE=oidc, via `openid-client`). Issues gateway
  JWTs (`JWT_SECRET`); `JwtAuthGuard` protects every other route.
- `conversations/` — CRUD for `conversations` + `GET /:id/messages`. Deleting
  a conversation also calls ingestion-service's
  `DELETE /conversations/:id/files` to clean up Milvus/NFS/side-store data.
  Also owns `PATCH /:id/system-prompt`, the conversation's custom
  instruction (see `shared/CONTRACTS.md` "system_prompt") — an empty/omitted
  value clears it back to `null`.
- `files/` — `POST /:id/files` streams a multipart upload to
  `ingestion-service POST /ingest` (via a short-lived temp file + `form-data`,
  no in-memory buffering of the whole file) and
  `GET /:id/files/:fileId/status` proxies `GET /ingest/{task_id}/status`.
- `query/` — `POST /:id/query` fetches the conversation's `system_prompt`,
  forwards it (plus the request's optional `images`, up to 4 base64 data
  URLs) to `POST /query` on retrieval-orchestrator, forwards the
  `text/event-stream` response to the client byte-for-byte, and — in
  parallel — parses `token`/`citation`/`done` frames (the `status`/
  `reasoning` events pass through untouched, unparsed) to persist the user
  question + attachments up front and the assistant reply (content +
  citations) once `done` arrives. `main.ts` raises the default Express body
  limit to 25mb to fit base64-encoded images.
- `common/database` — single `mssql.ConnectionPool`-backed `DatabaseService`.
- `common/http` — `IngestionClient` / `RetrievalClient`, thin axios wrappers
  around the two upstream Python services.
- `common/sse` — tiny incremental SSE frame parser used by `query/`.

## Known limitations (documented, not accidental)

- The `file_id -> task_id` map (`files/files.service.ts`) and the OIDC
  `state -> {nonce, code_verifier}` map (`auth/auth.service.ts`) are
  in-process `Map`s. Fine for a single gateway instance; move to Redis
  (`REDIS_URL` is already provisioned) before running multiple replicas.
- `GET /auth/callback` returns the JWT session as JSON (same shape as
  `/auth/dev-login`) rather than redirecting with a cookie — simplest
  integration for an SPA client; revisit if the IdP requires a strict
  redirect-only `redirect_uri`.
- OIDC mode depends on a real IdP (`OIDC_ISSUER`/`OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET`)
  and is therefore implemented in full but untested in this environment —
  `AUTH_MODE=dev` is what's exercised in local/dev/CI.

## Env vars

Repo-root `/.env.example` covers the shared ones (`MSSQL_HOST`/`MSSQL_PORT`/`MSSQL_DATABASE`/`MSSQL_USER`/`MSSQL_PASSWORD`, `AUTH_MODE`,
`JWT_SECRET`, `OIDC_*`, ...). `gateway/.env.example` documents the gateway-only
ones (`INGESTION_SERVICE_URL`, `RETRIEVAL_SERVICE_URL`, `GATEWAY_BASE_URL`,
`CLIENT_APP_URL`, `PORT`, `UPLOAD_TMP_DIR`, `MAX_UPLOAD_SIZE_BYTES`). The
gateway loads `.env` then falls back to `../.env` (`ConfigModule.forRoot`),
so either a gateway-local `.env` or the shared repo-root one works.

## Running

```bash
npm install
npm run build
npm run start:dev   # or: npm run start (runs dist/main.js)
```

Requires MSSQL reachable via `MSSQL_HOST`/`MSSQL_PORT`/`MSSQL_DATABASE`/
`MSSQL_USER`/`MSSQL_PASSWORD` (see repo-root `docker-compose.yml` for the
local dev container) and, for full end-to-end flows, `ingestion-service` on
`:8000` and `retrieval-orchestrator` on `:8001`.

## Endpoints

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/auth/dev-login` | — | `{username}` → JWT. Only when `AUTH_MODE=dev`. |
| GET | `/auth/login` | — | Redirects to IdP. Only when `AUTH_MODE=oidc`. |
| GET | `/auth/callback` | — | IdP redirect target → JWT. Only when `AUTH_MODE=oidc`. |
| GET | `/conversations` | JWT | Current user's conversations, `last_active_at desc`. |
| POST | `/conversations` | JWT | `{title?, retentionDays?}`. |
| GET | `/conversations/:id` | JWT | |
| DELETE | `/conversations/:id` | JWT | Also purges ingestion-service data. |
| PATCH | `/conversations/:id/system-prompt` | JWT | `{system_prompt?}` — empty/omitted clears it. |
| GET | `/conversations/:id/messages` | JWT | |
| POST | `/conversations/:id/files` | JWT | multipart `file` field → `{task_id, file_id}`. |
| GET | `/conversations/:id/files/:fileId/status` | JWT | optional `?taskId=` query param. |
| POST | `/conversations/:id/query` | JWT | `{question, thinking_level?, images?}` → SSE (`token`/`citation`/`status`/`reasoning`/`done`). `question` may be empty only if `images` is non-empty. |
| GET | `/health` | — | liveness probe. |
