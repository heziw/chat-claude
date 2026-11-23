# System Contracts (shared across gateway, services, client)

מקור יחיד לאמת לכל הצוותים/שירותים. כל שינוי בחוזים האלה דורש עדכון כאן ראשון.

## Relational DB: Microsoft SQL Server (database: `chatdb`)

**שונה מגרסה קודמת של המסמך**: המערכת רצה במקור מול Postgres; הוחלף ל-**MSSQL** כדי להתחבר לשרת הארגוני הקיים (לא פורסים DB חדש בפרודקשן — ראו `infra/helm/chat-system/README.md`). החלטות תרגום scheme שחלות על כל הטבלאות:
- **IDs מיוצרים באפליקציה, לא ב-DB**: עמודות `id` הן `NVARCHAR(36)` (לא `UNIQUEIDENTIFIER` הבינארי של MSSQL) **בלי** default — כל שירות מייצר `str(uuid4())`/`randomUUID()` לפני ה-INSERT ושולח אותו במפורש. זה נמנע לגמרי מהבדלי `RETURNING id` (Postgres) מול `OUTPUT inserted.id` (MSSQL), ושומר את אותו פורמט מחרוזת UUID מקווקו בדיוק כמו שכבר בשימוש בכל מקום אחר במערכת (שמות partition ב-Milvus, נתיבי NFS, וכו') — שום קוד שצורך UUID כ-string לא צריך להשתנות.
- `TIMESTAMPTZ`/`now()` → `DATETIMEOFFSET`/`SYSDATETIMEOFFSET()`.
- `JSONB` → `NVARCHAR(MAX)` (טקסט JSON גולמי; אין native JSON type ב-MSSQL) — כל שירות עושה serialize/deserialize מפורש (`json.dumps`/`JSON.stringify` לפני כתיבה, `json.loads`/`JSON.parse` אחרי קריאה) לעמודת `citations`.
- `TEXT` → `NVARCHAR(MAX)` (שדות ארוכים/לא חסומים) או `NVARCHAR(n)` (שדות קצרים עם גבול ידוע כמו `role`/`status`/`file_type`).

```sql
CREATE TABLE users (
  id NVARCHAR(36) PRIMARY KEY,
  sso_subject NVARCHAR(255) UNIQUE NOT NULL,
  display_name NVARCHAR(255) NOT NULL,
  role NVARCHAR(50) NOT NULL DEFAULT 'user',
  created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET()
);

CREATE TABLE conversations (
  id NVARCHAR(36) PRIMARY KEY,
  user_id NVARCHAR(36) NOT NULL REFERENCES users(id),
  title NVARCHAR(500) NOT NULL DEFAULT N'שיחה חדשה',
  retention_days INT NOT NULL DEFAULT 90,
  last_active_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET(),
  status NVARCHAR(20) NOT NULL DEFAULT 'active', -- active | purged
  created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET()
);

CREATE TABLE messages (
  id NVARCHAR(36) PRIMARY KEY,
  conversation_id NVARCHAR(36) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role NVARCHAR(20) NOT NULL, -- user | assistant | system
  content NVARCHAR(MAX) NOT NULL,
  citations NVARCHAR(MAX) NOT NULL DEFAULT '[]', -- JSON text, (de)serialized by the app
  created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET()
);

CREATE TABLE file_metadata (
  id NVARCHAR(36) PRIMARY KEY,
  conversation_id NVARCHAR(36) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  original_name NVARCHAR(500) NOT NULL,
  file_type NVARCHAR(20) NOT NULL, -- pdf | docx | xlsx | csv | json | image
  nfs_path NVARCHAR(1000) NOT NULL,
  size_bytes BIGINT NOT NULL,
  index_status NVARCHAR(20) NOT NULL DEFAULT 'pending', -- pending | parsing | chunking | embedding | indexing | writing | ready | failed
  milvus_partition NVARCHAR(100) NULL,
  structured_store_ref NVARCHAR(1000) NULL, -- path to parquet side-store for xlsx/csv/json
  error_message NVARCHAR(MAX) NULL,
  created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET()
);
```

**Drivers**: Python services use `python-tds` (pytds — pure-Python TDS client, no ODBC/system driver needed, consistent with the project's minimal-dependency-footprint preference for an air-gapped registry). Gateway (NestJS) uses the `mssql` npm package (tedious-based).

**Env vars** (replace the old single `POSTGRES_URL`): `MSSQL_HOST`, `MSSQL_PORT` (default `1433`), `MSSQL_DATABASE`, `MSSQL_USER`, `MSSQL_PASSWORD`. Local dev: a disposable `mcr.microsoft.com/mssql/server` container (see `docker-compose.yml`). Production: point these at the existing enterprise SQL Server — nothing in this repo deploys an MSSQL instance for production (see Helm chart README, same pattern Postgres had before).

Partition/collection naming: Milvus partition name = `conv_<conversation_id без מקפים>` (UUID hex, Milvus לא מאפשר מקפים בשמות partition).

## Milvus collection schema (collection name: `doc_chunks`)

| field | type | notes |
|---|---|---|
| `chunk_id` | VARCHAR(64), primary key | `<file_id>_<chunk_index>` |
| `conversation_id` | VARCHAR(36) | לשימוש ב-partition routing וב-filter גיבוי |
| `file_id` | VARCHAR(36) | FK ל-`file_metadata.id` |
| `file_type` | VARCHAR(16) | |
| `source_ref` | VARCHAR(256) | "עמוד 12" / "שורה 45" / "תא B7" — לציטוטים |
| `text` | VARCHAR(8192) | תוכן ה-chunk הגולמי |
| `dense_vector` | FLOAT_VECTOR(1024) | מ-`intfloat/multilingual-e5-large` |
| `sparse_vector` | SPARSE_FLOAT_VECTOR | BM25-style term weights |

Partition לכל conversation. Index: `HNSW` על dense_vector (cosine), sparse index מובנה של Milvus ל-sparse_vector.

## שירותים — API contracts (כולם FastAPI, OpenAPI זמין ב-`/openapi.json`)

### ingestion-service (`:8000`)
- `POST /ingest` — multipart form: `file`, `conversation_id`. מחזיר `{task_id, file_id}` (202 Accepted). מריץ pipeline אסינכרוני ב-Celery: save-to-nfs → parse → chunk → embed → write-milvus/mssql (ל-pdf/docx/image) **או** save-to-nfs → parse-to-dataframe → write-side-store (ל-xlsx/csv/json).
- `GET /ingest/{task_id}/status` → `{status, file_id, error}`. `status` is whatever `file_metadata.index_status` currently holds — the client should treat any value other than `ready`/`failed` as "still in progress" rather than enumerating an exact list, since the in-progress vocabulary is intentionally allowed to grow (currently: `pending` → `parsing` → `chunking` → `embedding` → `indexing` for pdf/docx/image, or `pending` → `parsing` → `writing` for xlsx/csv/json — real sequential sub-stages the ingestion pipeline already goes through, reported for UI transparency; see `ingestion-service/README.md`). Only `ready`/`failed` are ever checked elsewhere in the system (routing, retention, etc.).
- `DELETE /conversations/{conversation_id}/files` — מוחק partition ב-Milvus + קבצים ב-NFS + side-stores (נקרא גם ע"י retention-sweeper וגם ע"י gateway ב-מחיקה ידנית)

### retrieval-orchestrator (`:8001`)
- `POST /query` — body: `{conversation_id, question, history?, thinking_level?}`. Response: `text/event-stream` (SSE) עם events: `token` (delta טקסט), `citation` (source_ref+snippet), `status` (ראו למטה), `reasoning` (ראו למטה), `done`. מנותב (`classify_query`) ל-1 מתוך 5: **general** (אין שום קובץ בשיחה — override מכני, לפני כל סיווג אחר — שיחה רגילה בלי retrieval, בלי "לא נמצא הקשר" שעלול להטות את המודל לסרב לענות, בלי citations), **factual** (ברירת מחדל כש-general לא רלוונטי), **structured**, **summarize**, **multi**.

**`status` events** — אפס או יותר, לפני תחילת ה-`token` events, `data` הוא מחרוזת תווית בעברית (לא JSON) לתצוגה בלבד — למשל `"מחפש ומנתח מידע רלוונטי..."`, `"מתכנן ומריץ שאילתה על הנתונים..."`, `"מפרק את השאלה להשוואה..."`. משקפים גבולות אורקסטרציה אמיתיים שכבר קיימים בקוד (לא גרנולריות מזויפת) — לכל נתיב יש סט קבוע קטן משלו, ראו `retrieval-orchestrator/README.md`. **לא נשמרים** ב-MSSQL — זמינים רק בזמן ה-stream החי, לא כשחוזרים להיסטוריית שיחה ישנה.

**`reasoning` event** — אפס או אחד, לפני תחילת ה-`token` events (אחרי ה-`status` events), `data` הוא JSON עם `{route, ...}` — תוכן תלוי-נתיב:
- `structured`: `{route: "structured", steps: [{description, code}], judge?: {satisfactory, reason, corrected}}`
- `summarize`: `{route: "summarize", file: "<original_name>", chunk_count, judge?: {...}}`
- `multi`: `{route: "multi", sub_questions: [...], judge?: {satisfactory, reason, missing_file_ids, corrected?}}`
`judge` מופיע רק כש-`thinking_level="deep"`. `factual`/`general` לא מנפיקים `reasoning` בכלל (אין תוכן "חשיבה" מעבר ל-citations שכבר מוצגים). **לא נשמר** ב-MSSQL — אותה הערה כמו `status`.
- `GET /files/{file_id}/schema` — proxy ל-structured-data-agent (debug)

### structured-data-agent (`:8002`)
- `POST /structured-query` — body: `{file_id, question, thinking_level?}`. מריץ NL→תוכנית pandas (1-4 שלבים, כל שלב יכול להשתמש במשתנים ששלבים קודמים יצרו) בסנדבוקס, עם retry per-step (לא per-plan) על כשל. מחזיר `{answer, code_executed, result_table?, steps, judge?}` — `steps` הוא `[{description, code}]` לכל שלב שבפועל רץ בהצלחה (לאחר תיקון אם היה צורך), לשקיפות/דיבוג; `judge` מופיע רק כש-`thinking_level="deep"` (ראו למטה). צרכנים קיימים (כמו retrieval-orchestrator) ממשיכים לקרוא רק את `answer`.
- `GET /schema/{file_id}` → עמודות/טיפוסים של ה-DataFrame

### summarization-service (`:8003`)
- `POST /summarize` — body: `{file_id, thinking_level?, question?}`. מחזיר `{task_id}` (202). מריץ map-reduce היררכי ב-Celery; `question` (אם יש בו מוקד ספציפי, לא "סכם בבקשה" גנרי) ממקד את שלב ה-map בנושא שנשאל.
- `GET /summarize/{task_id}` → `{status, summary?, judge?}` — `judge` מופיע רק כש-`thinking_level="deep"` (ראו למטה).

### gateway (NestJS, `:3000`) — מול ה-client
- `POST /auth/login`, `GET /auth/callback` (OIDC) — dev מצב: `POST /auth/dev-login` (username בלבד, ללא IdP, **רק כשFLAG=dev**)
- `GET /conversations`, `POST /conversations`, `GET /conversations/:id`, `DELETE /conversations/:id`
- `GET /conversations/:id/messages`
- `POST /conversations/:id/files` (multipart proxy ל-ingestion-service `/ingest`)
- `GET /conversations/:id/files/:fileId/status` (proxy ל-ingestion-service, גם SSE push דרך `/conversations/:id/events`)
- `POST /conversations/:id/query` → SSE proxy ל-retrieval-orchestrator `/query`, שומר message+citations ב-MSSQL אחרי `done`

## thinking_level (נבחר ע"י המשתמש, לא קבוע מראש)

שדה אופציונלי (`"fast"|"standard"|"deep"`, ברירת מחדל `"standard"`) שעובר מה-client (בורר ב-UI ליד תיבת השאלה) → gateway → `POST /query` → לכל שירות שהניתוב בחר. **שני מנגנונים נפרדים, לא אחד**:

1. **reasoning_effort פשוט** — `thinking_level` מתורגם ל-`reasoning_effort` (`low`/`medium`/`high`) ומצורף לכל קריאת `/chat/completions` אמיתית (generation הסופי, `decompose_query` ב-multi, `generate_plan`/`regenerate_step` ב-structured, `summarize_text` ב-summarize). זו הנחה מתועדת על שם השדה שמודל reasoning מאחורי `VLLM_*_URL` מצפה לו (בהשראת `reasoning_effort` של OpenAI); יש לאמת מול קונפיגורציית ה-serving בפועל לפני מעבר ל-`MOCK_LLM=false`. `classify_query`'s הניתוב **לא** מקבל reasoning_effort מוגבר — זה סיווג מכני זול, לא בעיה שדורשת "חשיבה עמוקה".
2. **judge + replan, רק כש-`thinking_level="deep"`** — בשלושת הנתיבים (structured/summarize/multi), כל אחד עם failure mode משלו, אבל כולם מוגבלים לאותה צורה: **judge call אחד + replan בודד אחד** (לא agent loop פתוח), והתוצאה המקורית נשמרת אם ה-replan לא עוזר/נכשל:
   - **structured** (`app/llm.py:judge_result`) — אחרי שהתוכנית רצה בהצלחה, בודק (mock: None/NaN/Inf/ריק; real: שיפוט סמנטי) אם התוצאה אמינה ביחס לשאלה (למשל יחס עם מכנה אפס → NaN/Inf שקט שעבר "בהצלחה"). אם לא — `generate_plan` נקרא שוב עם הסיבה כ-`extra_context`, רץ עם אותו מנגנון per-step-retry.
   - **summarize** (`app/summarizer.py:judge_summary`, summarization-service) — בודק אם הסיכום הסופי מנוון/ריק (chunks שסוננו לגמרי, ריקים), ואם יש מוקד ספציפי לשאלה (לא "סכם בבקשה" גנרי) — אם הוא לא מופיע בסיכום. אם לא satisfactory — `hierarchical_summarize` רץ שוב פעם אחת עם הסיבה כ-`extra_context` שמתווסף להנחיית ה-map. הערה: ב-mock mode הבדיקה השנייה (coverage של המוקד) כמעט תמיד עוברת טריוויאלית כי `_mock_summarize` משכפל את ה-instruction (כולל המוקד) ישירות לתוך הפלט — משמעותית יותר ב-real mode שבו מודל אמיתי יכול להשמיט מוקד שהתבקש.
   - **multi** (`app/retrieval.py:judge_multi_coverage`, retrieval-orchestrator) — בדיקה מכנית בלבד (בלי קריאת LLM אפילו ב-real mode): האם כל קובצי ה-embedding שבשיחה מיוצגים ב-`merged_chunks` בפועל (כי `decompose_query` מפיק תתי-שאלות **בלתי-תלויות**, לא שרשרת, וייתכן ששתיים מהן "נופלות" על אותו מסמך). אם לא — `retrieve()` נקרא ישירות עם `file_id` ספציפי לכל מסמך חסר (hybrid search מסונן לקובץ בודד, עוקף את ה-decomposition), התוצאות מתמזגות עם הקיים, ממשיך ל-generation.

כל שלושת ה-judge-ים אינם חלק מחוזה ה-SSE של `/query` (`token`/`citation`/`done` בלבד) — נרשמים ללוג לצורכי דיבוג/שקיפות, וזמינים ב-JSON הגולמי של השירותים הפנימיים (`judge` ב-`/structured-query`, `judge` ב-`GET /summarize/{task_id}`), לא מוזרמים ל-client.

## Mock mode (לפיתוח מקומי בלי GPU/vLLM אמיתי)

כל קריאה ל-vLLM (embedding/rerank/router/generation/vision) עוברת דרך מודול `llm_clients.py` (Python) / `LlmClientsService` (Nest) שבודק env var `MOCK_LLM=true`:
- embeddings מדומים: hash דטרמיניסטי של הטקסט → וקטור פסאודו-רנדומלי קבוע (לבדיקת pipeline, לא relevance אמיתי)
- generation מדומה: מחזיר טקסט תבנית שמשרשר את ה-context שנשלף (לבדיקת E2E flow)
זה מאפשר להרים את כל המערכת לוקאלית ולבדוק flow מלא בלי GPU. ב-production (`MOCK_LLM=false`), הקריאות הולכות ל-endpoints האמיתיים ב-env vars: `VLLM_GENERATION_URL` (muse30b/gemma3), `VLLM_EMBED_URL`, `VLLM_RERANK_URL`, `VLLM_ROUTER_URL`, `VLLM_VISION_URL` — כולם OpenAI-compatible (`/v1/...`).

## Env vars (קובץ `.env`, ראו `.env.example`)
`MSSQL_HOST`, `MSSQL_PORT`, `MSSQL_DATABASE`, `MSSQL_USER`, `MSSQL_PASSWORD`, `MILVUS_HOST`, `MILVUS_PORT`, `REDIS_URL`, `NFS_ROOT_PATH`, `MOCK_LLM`, `VLLM_GENERATION_URL`, `VLLM_EMBED_URL`, `VLLM_RERANK_URL`, `VLLM_ROUTER_URL`, `VLLM_VISION_URL`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `JWT_SECRET`, `AUTH_MODE` (`oidc`|`dev`)
