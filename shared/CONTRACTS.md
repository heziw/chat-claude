# System Contracts (shared across gateway, services, client)

מקור יחיד לאמת לכל הצוותים/שירותים. כל שינוי בחוזים האלה דורש עדכון כאן ראשון.

## Postgres schema (database: `chatdb`)

```sql
CREATE TABLE users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sso_subject TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE conversations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id),
  title TEXT NOT NULL DEFAULT 'שיחה חדשה',
  retention_days INT NOT NULL DEFAULT 90,
  last_active_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  status TEXT NOT NULL DEFAULT 'active', -- active | purged
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL, -- user | assistant | system
  content TEXT NOT NULL,
  citations JSONB NOT NULL DEFAULT '[]',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE file_metadata (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  original_name TEXT NOT NULL,
  file_type TEXT NOT NULL, -- pdf | docx | xlsx | csv | json | image
  nfs_path TEXT NOT NULL,
  size_bytes BIGINT NOT NULL,
  index_status TEXT NOT NULL DEFAULT 'pending', -- pending | indexing | ready | failed
  milvus_partition TEXT,
  structured_store_ref TEXT, -- path to parquet/duckdb side-store for xlsx/csv/json
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

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
- `POST /ingest` — multipart form: `file`, `conversation_id`. מחזיר `{task_id, file_id}` (202 Accepted). מריץ pipeline אסינכרוני ב-Celery: save-to-nfs → parse → chunk → embed → write-milvus/postgres (ל-pdf/docx/image) **או** save-to-nfs → parse-to-dataframe → write-side-store (ל-xlsx/csv/json).
- `GET /ingest/{task_id}/status` → `{status, file_id, error}` (status ∈ pending|indexing|ready|failed)
- `DELETE /conversations/{conversation_id}/files` — מוחק partition ב-Milvus + קבצים ב-NFS + side-stores (נקרא גם ע"י retention-sweeper וגם ע"י gateway ב-מחיקה ידנית)

### retrieval-orchestrator (`:8001`)
- `POST /query` — body: `{conversation_id, question, history?, thinking_level?}`. Response: `text/event-stream` (SSE) עם events: `token` (delta טקסט), `citation` (source_ref+snippet), `done`.
- `GET /files/{file_id}/schema` — proxy ל-structured-data-agent (debug)

### structured-data-agent (`:8002`)
- `POST /structured-query` — body: `{file_id, question, thinking_level?}`. מריץ NL→תוכנית pandas (1-4 שלבים, כל שלב יכול להשתמש במשתנים ששלבים קודמים יצרו) בסנדבוקס, עם retry per-step (לא per-plan) על כשל. מחזיר `{answer, code_executed, result_table?, steps, judge?}` — `steps` הוא `[{description, code}]` לכל שלב שבפועל רץ בהצלחה (לאחר תיקון אם היה צורך), לשקיפות/דיבוג; `judge` מופיע רק כש-`thinking_level="deep"` (ראו למטה). צרכנים קיימים (כמו retrieval-orchestrator) ממשיכים לקרוא רק את `answer`.
- `GET /schema/{file_id}` → עמודות/טיפוסים של ה-DataFrame

### summarization-service (`:8003`)
- `POST /summarize` — body: `{file_id, thinking_level?}`. מחזיר `{task_id}` (202). מריץ map-reduce היררכי ב-Celery.
- `GET /summarize/{task_id}` → `{status, summary?}`

### gateway (NestJS, `:3000`) — מול ה-client
- `POST /auth/login`, `GET /auth/callback` (OIDC) — dev מצב: `POST /auth/dev-login` (username בלבד, ללא IdP, **רק כשFLAG=dev**)
- `GET /conversations`, `POST /conversations`, `GET /conversations/:id`, `DELETE /conversations/:id`
- `GET /conversations/:id/messages`
- `POST /conversations/:id/files` (multipart proxy ל-ingestion-service `/ingest`)
- `GET /conversations/:id/files/:fileId/status` (proxy ל-ingestion-service, גם SSE push דרך `/conversations/:id/events`)
- `POST /conversations/:id/query` → SSE proxy ל-retrieval-orchestrator `/query`, שומר message+citations ב-Postgres אחרי `done`

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
`POSTGRES_URL`, `MILVUS_HOST`, `MILVUS_PORT`, `REDIS_URL`, `NFS_ROOT_PATH`, `MOCK_LLM`, `VLLM_GENERATION_URL`, `VLLM_EMBED_URL`, `VLLM_RERANK_URL`, `VLLM_ROUTER_URL`, `VLLM_VISION_URL`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `JWT_SECRET`, `AUTH_MODE` (`oidc`|`dev`)
