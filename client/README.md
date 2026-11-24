# client

Angular 22 SPA — the chat UI. Talks only to the `gateway` (never directly to
the Python services or Milvus/MSSQL). See `/shared/CONTRACTS.md` for the
full system contract. Dev server on `:4200`.

## Running

Requires **Node >=22.22.3** (Angular 22's floor). `infra/docker/
client.Dockerfile` already targets `node:26-alpine`; for local dev on an
older system Node, grab a standalone build instead of upgrading
system-wide:

```bash
curl -sL -o /tmp/node.tar.xz https://nodejs.org/dist/latest-v26.x/node-v26.13.1-linux-x64.tar.xz
mkdir -p ~/.local/node && tar -xf /tmp/node.tar.xz -C ~/.local/node --strip-components=1
export PATH="$HOME/.local/node/bin:$PATH"   # add to your shell profile

npm install
npm start       # ng serve, :4200
npm run build   # production build to dist/
```

`npm-wrapper.sh` exists only so the preview tooling's dev-server launcher
(`.claude/launch.json`) picks up the portable Node above instead of
whatever system Node it would otherwise resolve — irrelevant outside that
tooling.

## Dependency pins worth knowing about

`@angular/*` was bumped 18 -> 22 (`npm audit` flagged 59 vulnerabilities —
19 high, 2 critical — all tracing to `@angular/cli`/`@angular-devkit/
build-angular` needing Angular 22) via `ng update` one major at a time
(18->19->20->21->22, the officially supported path — skipping majors isn't
safe, each version's migration schematics assume the previous one already
ran). Every step's automated migration was reviewed by hand, not blindly
accepted: the `standalone: true` -> implicit-default cleanup (19), the new
explicit `ChangeDetectionStrategy.Eager` on every component (22, preserves
exactly the previous implicit "Default" strategy's behavior under a new
explicit name), and `provideHttpClient(withXhr(), ...)` (22, keeps the
pre-existing `HttpXhrBackend`-based transport instead of silently switching
to the new `fetch()`-based default). `typescript` landed on `~6.0.3` as a
transitive requirement of this chain, same version gateway is pinned to.

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

## Chat screen (`features/conversations/chat/`)

Beyond message history and file upload, the composer (`chat.component.ts`)
has grown a few more pieces, all driven by `ChatService`'s signals:

- **`thinking_level` selector** — "fast"/"standard"/"deep", sent with every
  question; see `shared/CONTRACTS.md` "thinking_level" for what each level
  actually changes server-side.
- **Live status line + reasoning panel** — while a question is being
  answered, `processingStatus()` surfaces the backend's `status` SSE events
  as a short progress label; once answered, a collapsible "🧠" panel (only
  shown when the route emitted a `reasoning` event) shows the route's actual
  work — the pandas plan's steps, which file got summarized, the
  sub-questions a comparison was split into — plus a judge verdict when
  `thinking_level="deep"` ran one. Both are ephemeral: reloading an old
  conversation shows neither, only the final answer text + citations.
- **Conversation settings panel ("⚙️")** — edits the conversation's custom
  `system_prompt` (`PATCH /conversations/:id/system-prompt`), a persistent
  instruction resent on every future question in that conversation.
- **One attach button, routed by extension** (`handleFiles` in
  `chat.component.ts`) — `pdf`/`docx`/`xlsx`/`csv`/`json`/`tif`/`tiff` go
  through the existing `uploadFile()` → ingestion-service flow; any other
  `image/*` is base64-encoded client-side (`FileReader`, capped at 4 images /
  4MB each) and sent as `images` on the next question instead — synchronous,
  never indexed, only meaningful in a conversation with no files yet (see
  `retrieval-orchestrator/README.md`'s "`images`" section). Dropped files are
  routed the same way `onDrop` → `handleFiles`.
- **Quick-prompt chips** — a fixed row of canned questions ("📝 סכם", "📊 נתח
  נתונים", ...) that insert text into the composer, combinable with
  free-typed text. These are plain client-side strings, not a separate
  backend concept — they work because the usual `classify_query` routing
  picks up their keywords exactly as if the user had typed them.

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
