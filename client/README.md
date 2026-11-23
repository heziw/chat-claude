# client

Angular 18 SPA — the chat UI. Talks only to the `gateway` (never directly to
the Python services or Milvus/MSSQL). See `/shared/CONTRACTS.md` for the
full system contract. Dev server on `:4200`.

## Architecture choices

- **Standalone components, no NgModules** — every route is lazy-loaded
  (`app.routes.ts`, `loadComponent`).
- **Signals, not NgRx** — state is local to this single-page chat app
  (current conversation, message list, streaming text, file statuses); NgRx's
  actions/reducers/effects boilerplate isn't justified at this scope. If the
  app grows into something with real cross-feature/cross-tab shared state,
  that's the point to revisit this.
- **RTL Hebrew** — `dir="rtl"` is set on `<html>`/`<body>` in `index.html`;
  all UI copy is Hebrew.

## Structure

```
core/
  services/
    auth.service.ts           dev-login / JWT storage, signal-based current user
    conversations.service.ts  conversation CRUD against the gateway
    chat.service.ts           SSE streaming — see below
  guards/auth.guard.ts         redirects to /login without a token
  interceptors/auth.interceptor.ts   adds Authorization: Bearer <token> to HttpClient calls
  models/                      Conversation / Message / Citation / file types
features/
  login/                       dev-login screen (OIDC redirect flow: see TODO comment in auth.service.ts)
  conversations/
    shell/                     sidebar + outlet
    sidebar/                   conversation list
    empty-state/                "pick or create a conversation" placeholder
    chat/                      message history, upload, streaming Q&A — the main screen
```

## SSE streaming (`core/services/chat.service.ts`)

`EventSource` can't send a POST body, so streaming uses `fetch` +
`ReadableStream` with a hand-rolled SSE parser (`consumeSseStream` /
`handleRawEvent`). Two things here are subtle enough to be worth flagging
for whoever touches this file next:

- **Leading-space handling**: per the SSE spec, only a *single* leading
  space after `data:` is a protocol separator — the rest of the value
  (including further leading/trailing whitespace) is the actual payload and
  must be preserved. An earlier version used `.trim()` here, which silently
  ate the leading space on every token and glued streamed words together
  with no spaces between them.
- **Don't JSON-sniff token payloads**: token events are plain text by
  contract (see `retrieval-orchestrator`'s `_sse_event("token", delta)` —
  always a bare string, in both mock and real mode). An earlier version
  tried `JSON.parse()` on every token's data to detect an optional
  `{"delta": "..."}` envelope; the problem is a bare numeric/boolean/null
  token (e.g. an answer token that's just `"315000"`) is *also* valid JSON,
  so `JSON.parse("315000")` silently returns the number `315000`, which then
  matched neither the "is it a string" nor "is it an object" branch and got
  dropped — numeric answers (structured-data-agent's whole output type)
  would render with the number missing. Fixed by only attempting JSON
  parsing for the `citation` event (always a genuine `{...}` object), never
  for `token`.

## Env / config

`src/environments/environment.ts` (prod) and `environment.development.ts`
(dev, used by `ng serve`) set `apiBaseUrl` — the gateway's base URL. For the
air-gapped on-prem deployment this is expected to be overridden at build
time or served from the same origin as the gateway via a reverse proxy.

## Running

```bash
npm install
npm start       # ng serve, :4200, proxies nothing — apiBaseUrl must point at a running gateway
npm run build   # production build to dist/
```

Requires `gateway` reachable at `environment.apiBaseUrl` (default
`http://localhost:3000`).
