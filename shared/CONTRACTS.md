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
  system_prompt NVARCHAR(MAX) NULL, -- custom per-conversation instruction, see "system_prompt" below
  created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET()
);

CREATE TABLE messages (
  id NVARCHAR(36) PRIMARY KEY,
  conversation_id NVARCHAR(36) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role NVARCHAR(20) NOT NULL, -- user | assistant | system
  content NVARCHAR(MAX) NOT NULL,
  citations NVARCHAR(MAX) NOT NULL DEFAULT '[]', -- JSON text, (de)serialized by the app
  attachments NVARCHAR(MAX) NOT NULL DEFAULT '[]', -- JSON array of base64 image data URLs, see "images" below
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
- `POST /query` — body: `{conversation_id, question, history?, thinking_level?, system_prompt?, images?}`. Response: `text/event-stream` (SSE) עם events: `token` (delta טקסט), `citation` (source_ref+snippet), `status` (ראו למטה), `reasoning` (ראו למטה), `done`. מנותב (`classify_query`) ל-1 מתוך 5: **general** (אין שום קובץ בשיחה — override מכני, לפני כל סיווג אחר — שיחה רגילה בלי retrieval, בלי "לא נמצא הקשר" שעלול להטות את המודל לסרב לענות, בלי citations), **factual** (ברירת מחדל כש-general לא רלוונטי), **structured**, **summarize**, **multi**. ראו "system_prompt" ו-"images" למטה לשני השדות האופציונליים.

**`status` events** — אפס או יותר, לפני תחילת ה-`token` events, `data` הוא מחרוזת תווית בעברית (לא JSON) לתצוגה בלבד — למשל `"מחפש ומנתח מידע רלוונטי..."`, `"מתכנן ומריץ שאילתה על הנתונים..."`, `"מפרק את השאלה להשוואה..."`. משקפים גבולות אורקסטרציה אמיתיים שכבר קיימים בקוד (לא גרנולריות מזויפת) — לכל נתיב יש סט קבוע קטן משלו, ראו `retrieval-orchestrator/README.md`. **לא נשמרים** ב-MSSQL — זמינים רק בזמן ה-stream החי, לא כשחוזרים להיסטוריית שיחה ישנה.

**`reasoning` event** — אפס או אחד, לפני תחילת ה-`token` events (אחרי ה-`status` events), `data` הוא JSON עם `{route, ...}` — תוכן תלוי-נתיב:
- `structured`: `{route: "structured", steps: [{description, code}], judge?: {satisfactory, reason, corrected}}`
- `summarize`: `{route: "summarize", file: "<original_name>", chunk_count, judge?: {...}}`
- `multi`: `{route: "multi", sub_questions: [...], judge?: {satisfactory, reason, missing_file_ids, corrected?}}`
`judge` מופיע רק כש-`thinking_level="deep"`. `factual`/`general` לא מנפיקים `reasoning` בכלל (אין תוכן "חשיבה" מעבר ל-citations שכבר מוצגים). **לא נשמר** ב-MSSQL — אותה הערה כמו `status`.
- `GET /files/{file_id}/schema` — proxy ל-structured-data-agent (debug)

### structured-data-agent (`:8002`)
- `POST /structured-query` — body: `{file_id, question, thinking_level?, system_prompt?}`. מריץ NL→תוכנית pandas (1-4 שלבים, כל שלב יכול להשתמש במשתנים ששלבים קודמים יצרו) בסנדבוקס, עם retry per-step (לא per-plan) על כשל. מחזיר `{answer, code_executed, result_table?, steps, judge?}` — `steps` הוא `[{description, code}]` לכל שלב שבפועל רץ בהצלחה (לאחר תיקון אם היה צורך), לשקיפות/דיבוג; `judge` מופיע רק כש-`thinking_level="deep"` (ראו למטה). צרכנים קיימים (כמו retrieval-orchestrator) ממשיכים לקרוא רק את `answer`.
- `GET /schema/{file_id}` → עמודות/טיפוסים של ה-DataFrame

### summarization-service (`:8003`)
- `POST /summarize` — body: `{file_id, thinking_level?, question?, system_prompt?}`. מחזיר `{task_id}` (202). מריץ map-reduce היררכי ב-Celery; `question` (אם יש בו מוקד ספציפי, לא "סכם בבקשה" גנרי) ממקד את שלב ה-map בנושא שנשאל.
- `GET /summarize/{task_id}` → `{status, summary?, judge?}` — `judge` מופיע רק כש-`thinking_level="deep"` (ראו למטה).

### gateway (NestJS, `:3000`) — מול ה-client
- `POST /auth/login`, `GET /auth/callback` (OIDC) — dev מצב: `POST /auth/dev-login` (username בלבד, ללא IdP, **רק כשFLAG=dev**)
- `GET /conversations`, `POST /conversations`, `GET /conversations/:id`, `DELETE /conversations/:id`
- `PATCH /conversations/:id/system-prompt` — body: `{system_prompt?}` (מחרוזת ריקה/חסרה = איפוס ל-`null`). מעדכן `conversations.system_prompt`, מחזיר את השורה המעודכנת.
- `GET /conversations/:id/messages`
- `POST /conversations/:id/files` (multipart proxy ל-ingestion-service `/ingest`)
- `GET /conversations/:id/files/:fileId/status` (proxy ל-ingestion-service, גם SSE push דרך `/conversations/:id/events`)
- `POST /conversations/:id/query` — body: `{question, thinking_level?, images?}` (עד 4 data URLs, ראו "images" למטה; גוף הבקשה מוגדל ל-25mb ב-`main.ts` כדי להכיל אותם). `question` יכול להיות ריק **רק** אם `images` לא ריק (שליחת תמונה בלי כיתוב) — אחרת 400. שולף את `system_prompt` של השיחה מ-MSSQL בעצמו (לא מגיע מה-client) ומצרף אותו לבקשה ל-retrieval-orchestrator. → SSE proxy ל-retrieval-orchestrator `/query`, שומר message+citations+attachments ב-MSSQL אחרי `done`

## thinking_level (נבחר ע"י המשתמש, לא קבוע מראש)

שדה אופציונלי (`"fast"|"standard"|"deep"`, ברירת מחדל `"standard"`) שעובר מה-client (בורר ב-UI ליד תיבת השאלה) → gateway → `POST /query` → לכל שירות שהניתוב בחר. **שני מנגנונים נפרדים, לא אחד**:

1. **reasoning_effort פשוט** — `thinking_level` מתורגם ל-`reasoning_effort` (`low`/`medium`/`high`) ומצורף לכל קריאת `/chat/completions` אמיתית (generation הסופי, `decompose_query` ב-multi, `generate_plan`/`regenerate_step` ב-structured, `summarize_text` ב-summarize). זו הנחה מתועדת על שם השדה שמודל reasoning מאחורי `VLLM_*_URL` מצפה לו (בהשראת `reasoning_effort` של OpenAI); יש לאמת מול קונפיגורציית ה-serving בפועל לפני מעבר ל-`MOCK_LLM=false`. `classify_query`'s הניתוב **לא** מקבל reasoning_effort מוגבר — זה סיווג מכני זול, לא בעיה שדורשת "חשיבה עמוקה".
2. **judge + replan, רק כש-`thinking_level="deep"`** — בשלושת הנתיבים (structured/summarize/multi), כל אחד עם failure mode משלו, אבל כולם מוגבלים לאותה צורה: **judge call אחד + replan בודד אחד** (לא agent loop פתוח), והתוצאה המקורית נשמרת אם ה-replan לא עוזר/נכשל:
   - **structured** (`app/llm.py:judge_result`) — אחרי שהתוכנית רצה בהצלחה, בודק (mock: None/NaN/Inf/ריק; real: שיפוט סמנטי) אם התוצאה אמינה ביחס לשאלה (למשל יחס עם מכנה אפס → NaN/Inf שקט שעבר "בהצלחה"). אם לא — `generate_plan` נקרא שוב עם הסיבה כ-`extra_context`, רץ עם אותו מנגנון per-step-retry.
   - **summarize** (`app/summarizer.py:judge_summary`, summarization-service) — בודק אם הסיכום הסופי מנוון/ריק (chunks שסוננו לגמרי, ריקים), ואם יש מוקד ספציפי לשאלה (לא "סכם בבקשה" גנרי) — אם הוא לא מופיע בסיכום. אם לא satisfactory — `hierarchical_summarize` רץ שוב פעם אחת עם הסיבה כ-`extra_context` שמתווסף להנחיית ה-map. הערה: ב-mock mode הבדיקה השנייה (coverage של המוקד) כמעט תמיד עוברת טריוויאלית כי `_mock_summarize` משכפל את ה-instruction (כולל המוקד) ישירות לתוך הפלט — משמעותית יותר ב-real mode שבו מודל אמיתי יכול להשמיט מוקד שהתבקש.
   - **multi** (`app/retrieval.py:judge_multi_coverage`, retrieval-orchestrator) — בדיקה מכנית בלבד (בלי קריאת LLM אפילו ב-real mode): האם כל קובצי ה-embedding שבשיחה מיוצגים ב-`merged_chunks` בפועל (כי `decompose_query` מפיק תתי-שאלות **בלתי-תלויות**, לא שרשרת, וייתכן ששתיים מהן "נופלות" על אותו מסמך). אם לא — `retrieve()` נקרא ישירות עם `file_id` ספציפי לכל מסמך חסר (hybrid search מסונן לקובץ בודד, עוקף את ה-decomposition), התוצאות מתמזגות עם הקיים, ממשיך ל-generation.

כל שלושת ה-judge-ים אינם חלק מחוזה ה-SSE של `/query` (`token`/`citation`/`done` בלבד) — נרשמים ללוג לצורכי דיבוג/שקיפות, וזמינים ב-JSON הגולמי של השירותים הפנימיים (`judge` ב-`/structured-query`, `judge` ב-`GET /summarize/{task_id}`), לא מוזרמים ל-client.

## system_prompt (הנחיה מותאמת אישית לשיחה)

שדה טקסט אופציונלי **ברמת השיחה** (לא ברמת ההודעה) — `conversations.system_prompt`, נערך דרך פאנל "⚙️ הגדרות שיחה" בקליינט ו-`PATCH /conversations/:id/system-prompt`. בניגוד ל-`thinking_level` (נבחר מחדש בכל שאלה), זה נשמר ב-MSSQL ונשלח **אוטומטית מה-gateway** (לא מה-client) בכל `POST /query` לאורך כל חיי השיחה, עד שמשתנה/מתאפס.

מגיע עד לכל LLM אמיתי שבפועל עונה על השאלה — **בכל אחד מ-5 הנתיבים**, לא רק general/factual:
- **general/factual/multi** (`generation.py:generate_stream`) — מצורף כ-הודעת `{"role": "system", ...}` אמיתית, לפני הודעת ה-`user`.
- **structured** (`llm.py:_build_plan_prompt`) — מתווסף כשורת הנחיה בתוך prompt יצירת התוכנית (אין שם מנגנון system-role קיים להשתמש בו).
- **summarize** (`summarizer.py:_map_instruction`) — מתווסף להנחיית שלב ה-map, לצד המוקד של השאלה (אם יש).

ב-`MOCK_LLM=true` רק הנתיב הראשון (general/factual/multi) משקף אותו בפלט המדומה (prefix `[system prompt: ...]`) — structured/summarize ב-mock mode מתעלמים ממנו בפועל (ה-heuristic הדטרמיניסטי לא "קורא" טקסט חופשי), אבל השדה כן מועבר דרך כל השרשרת כדי לוודא שהצנרת לא נשברת.

## images (תמונות מצורפות לשיחה כללית)

שדה אופציונלי **ברמת ההודעה** (לא ברמת השיחה) — `data:image/<png|jpeg|jpg|gif|webp>;base64,...` עד 4 תמונות, מקודד ע"י הקליינט (`FileReader.readAsDataURL`, מוגבל גם ל-4MB לכל קובץ). זו יכולת **chat מולטימודלי אמיתי** (שליחת תמונה למודל כדי שיתאר/יענה עליה) — נפרדת לגמרי מ-pipeline ה-OCR הקיים להעלאת קבצי תמונה כ"מסמך" ל-RAG (`ingestion-service/app/parsing.py:parse_image`, pytesseract).

- נשמר ב-`messages.attachments` (JSON, כמו `citations`) כדי שהתמונה תוצג גם בטעינה מחדש של השיחה.
- מגיע עד ל-retrieval-orchestrator כ-`QueryRequest.images`, אבל **נעשה בו שימוש רק בנתיב ה-general** (`_handle_general`) — שיחה בלי קבצים כלל. בשאר הנתיבים השדה פשוט לא נקרא (לא שגיאה, סתם ignored), כי "שאל שאלה עם תמונה מצורפת" ו"יש קבצים מאונדקסים בשיחה" הם שני מצבים לא-קשורים.
- `generation.py:_real_generate_stream` בונה הודעת `user` מולטימודלית בפורמט OpenAI (`content: [{"type":"text",...}, {"type":"image_url","image_url":{"url": "data:..."}}, ...]`) ושולח ל-**`VLLM_VISION_URL`** (לא `VLLM_GENERATION_URL`) — דיפלוימנט נפרד למודל vision-capable, כי אין הבטחה ש-muse30b/gemma3 הראשי עצמו מולטימודלי. `VLLM_VISION_MODEL` (חדש) הוא שם המודל ב-payload; `VLLM_VISION_URL` כבר הוגדר קודם (היה unused placeholder).
- ב-`MOCK_LLM=true` מוחזר prefix `[כולל N תמונות מצורפות]` בפלט המדומה, לאימות שה-pipeline עובד בלי GPU.

**כפתור אחד בקליינט, ניתוב לפי סיומת** — `chat.component.ts` חושף כפתור "📎" יחיד (לא שניים); כל קובץ שנבחר מנותב לפי סיומת:
- `pdf`/`docx`/`xlsx`/`csv`/`json`/`tif`/`tiff` → `uploadFile()` הרגיל (ingestion-service, אינדוקס RAG a-synchroni).
- כל `image/*` אחר (png/jpg/jpeg/gif/bmp/webp) → `handleImageFiles()` — נכנס ל-`attachedImages`, מקודד ל-base64 ונשלח כ-`images` בשאלה הבאה (סינכרוני, לא נאינדקס).
- סוג קובץ אחר — שגיאה ("סוג הקובץ אינו נתמך"), בלי קריאת שרת.

הסיבה ל-`tif`/`tiff` בקבוצת ה-documents ולא ה-images: ראו "OCR מוגבל לסריקות" למטה — זה הפורמט היחיד שעדיין עובר OCR לאינדוקס.

## OCR מוגבל לסריקות + תיאור תמונות מוטבעות ב-PDF (ingestion-service)

**OCR רק על tif/tiff, לא על כל תמונה** — `parsing.py:parse_file` מבחין בין `_OCR_SCAN_IMAGE_EXTENSIONS = {tif, tiff}` (עובר ל-`parse_image`, OCR כרגיל) לבין `_NON_SCAN_IMAGE_EXTENSIONS = {image, jpg, jpeg, png, bmp, gif, webp}` (מחזיר `[]` — אפס chunks, בלי ניסיון OCR בכלל). הנימוק: TIFF הוא כמעט תמיד פלט של סורק/פקס/ארכיון, לעומת תמונת מצלמה/screenshot שכמעט אף פעם לא מכילה טקסט מסמך אמיתי לשחזר. הקליינט ממילא לא שולח יותר תמונות לא-TIFF ל-`/ingest` (ראו למעלה) — זו הגנה דפנסיבית בלבד למי שקורא ל-API ישירות.

**תיאור (captioning) תמונות מוטבעות בתוך עמוד PDF עם טקסט אמיתי** — נפרד לגמרי מה-OCR לעמוד-סרוק-שלם (שמופעל רק כשלעמוד *כולו* אין שכבת טקסט). `parsing.py:_extract_pdf_pages`/`_caption_embedded_images`: לכל עמוד PDF שבו **כבר חולץ טקסט אמיתי** (לא OCR), עובר על כל התמונות המוטבעות בעמוד (`pypdf`'s `page.images`) ו:
- מדלג על תמונה קטנה בשני הממדים (ברירת מחדל: מתחת ל-80px) — כנראה לוגו/אייקון/סימון דקורטיבי, לא שווה קריאת מודל vision.
- עד 5 תמונות לעמוד (הגנה מפני עמוד עמוס-תמונות שמייצר עשרות קריאות vision).
- לכל תמונה שעברה את הסינון: `app/vision.py:caption_image` שולח אותה (base64, OpenAI content-block) ל-**`VLLM_VISION_URL`**/`VLLM_VISION_MODEL` (אותו endpoint/שם-מודל חדש כמו ב-retrieval-orchestrator, אבל דיפלוימנט/קריאה נפרדים — זה השימוש הראשון שבפועל נעשה ב-`VLLM_VISION_URL` של **ingestion-service**, שהיה placeholder לא-בשימוש עד עכשיו) ומקבל תיאור טקסטואלי קצר.
- כל תיאור הופך ל-`Chunk` נפרד עם `source_ref` ייעודי: `"עמוד N (תמונה מוטבעת)"` (מובחן מ-`"עמוד N"` הרגיל של הטקסט), כדי שציטוט שמצביע על תמונה מתוארת יהיה ברור כזה ולא ייראה כאילו הגיע מהטקסט הרץ של העמוד.
- ב-`MOCK_LLM=true`: `caption_image` מחזיר `"[תיאור מדומה של תמונה מוטבעת, N bytes]"` דטרמיניסטי, בלי קריאת רשת.
- Best-effort מלא: תמונה שנכשלת ב-captioning (או עמוד עם תמונות ש-`pypdf` לא מצליח לקרוא) פשוט לא מייצרת chunk — אף פעם לא מפילה את כל אינדוקס המסמך.

## Mock mode (לפיתוח מקומי בלי GPU/vLLM אמיתי)

כל קריאה ל-vLLM (embedding/rerank/router/generation/vision) עוברת דרך מודול `llm_clients.py` (Python) / `LlmClientsService` (Nest) שבודק env var `MOCK_LLM=true`:
- embeddings מדומים: hash דטרמיניסטי של הטקסט → וקטור פסאודו-רנדומלי קבוע (לבדיקת pipeline, לא relevance אמיתי)
- generation מדומה: מחזיר טקסט תבנית שמשרשר את ה-context שנשלף (לבדיקת E2E flow)
זה מאפשר להרים את כל המערכת לוקאלית ולבדוק flow מלא בלי GPU. ב-production (`MOCK_LLM=false`), הקריאות הולכות ל-endpoints האמיתיים ב-env vars: `VLLM_GENERATION_URL` (muse30b/gemma3), `VLLM_EMBED_URL`, `VLLM_RERANK_URL`, `VLLM_ROUTER_URL`, `VLLM_VISION_URL` — כולם OpenAI-compatible (`/v1/...`).

## Env vars (קובץ `.env`, ראו `.env.example`)
`MSSQL_HOST`, `MSSQL_PORT`, `MSSQL_DATABASE`, `MSSQL_USER`, `MSSQL_PASSWORD`, `MILVUS_HOST`, `MILVUS_PORT`, `REDIS_URL`, `NFS_ROOT_PATH`, `MOCK_LLM`, `VLLM_GENERATION_URL`, `VLLM_EMBED_URL`, `VLLM_RERANK_URL`, `VLLM_ROUTER_URL`, `VLLM_VISION_URL`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `JWT_SECRET`, `AUTH_MODE` (`oidc`|`dev`)
