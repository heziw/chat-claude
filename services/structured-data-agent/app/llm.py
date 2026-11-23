"""NL -> pandas code generation, following the same MOCK_LLM contract as
the rest of the system (see shared/CONTRACTS.md "Mock mode").

Two APIs, both used by ``app/main.py``'s plan -> execute-step -> retry loop
(a single aggregation question is just the common case of a 1-step plan;
there is no separate "simple" code path to keep in sync with the
multi-step one):

- ``generate_plan(question, schema_description, thinking_level, extra_context)``
  -> ``list[{"description", "code"}]``, 1-4 steps. Earlier steps may define
  intermediate variables/columns that later steps use (e.g. step 1 computes
  a ratio column, step 2 sorts by it); only the *last* step is required to
  assign the final answer to a variable named ``result``. ``extra_context``
  is set only when ``judge_result()`` (below) flagged a first attempt as
  unsatisfactory and ``app/main.py`` is replanning from scratch with that
  reason folded into the prompt.
- ``regenerate_step(question, schema_description, prior_steps, failed_step,
  error, thinking_level)`` -> a single corrected ``{"description", "code"}``,
  used to retry just the step that failed (not the whole plan) with the
  error fed back.
- ``judge_result(question, schema_description, steps, result_display,
  thinking_level)`` -> ``{"satisfactory": bool, "reason": str}``. Only ever
  called by ``app/main.py`` when ``thinking_level == "deep"`` -- see
  CONTRACTS.md "thinking_level" for why this is gated and bounded to one
  judge + one replan attempt rather than an open-ended loop.

``thinking_level`` (``"fast"|"standard"|"deep"``, default ``"standard"``) is
forwarded as a ``reasoning_effort`` field (``"low"|"medium"|"high"``) on
every real-mode chat-completion call -- see ``_reasoning_effort()``. This is
a documented *assumption* about what field name a reasoning-tuned model
served behind ``VLLM_ROUTER_URL`` expects (mirrors OpenAI's o-series
``reasoning_effort`` convention); confirm against the actual model's serving
config before relying on it. Mock mode ignores ``thinking_level`` entirely
for code/plan generation (the heuristic is deterministic either way) but
``_mock_judge`` and ``_mock_plan``'s ``extra_context`` handling below *do*
behave differently, so the judge/replan loop is still genuinely exercisable
without a GPU.

``MOCK_LLM=true`` behavior: a small keyword-based heuristic over the
question text. Most questions get the original 1-step path (sum/mean/max/
min/count over the first numeric column found in ``schema_description``, or
``df.describe()``); a question that mentions two numeric columns together
with a ratio/superlative phrasing ("X per Y", "highest ... ratio") gets a
genuine 2-step plan (compute the ratio column, then pick the row with the
highest/lowest value). This deliberately is NOT meant to understand
arbitrary questions -- it exists so mock-mode tests exercise a real,
verifiable multi-step computation end-to-end instead of an empty stub.
Step-retry in mock mode has no notion of "fixing" deterministic code, so it
just falls back to ``df.describe()``, which always succeeds.

``MOCK_LLM=false`` behavior: calls the OpenAI-compatible ``VLLM_ROUTER_URL``
``/chat/completions`` endpoint, instructing the model to reply with *only*
a JSON object (optionally fenced). Any failure to reach the endpoint, or to
parse a usable plan/step out of the response, falls back to the mock
heuristic's output for that call rather than erroring -- better a
best-effort single-step answer than a hard failure on a transient router
issue.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_NUMERIC_DTYPE_HINTS = ("int", "float", "double")

_SUM_KEYWORDS = ("sum", "total", "סכום", "סך")
_AVG_KEYWORDS = ("average", "avg", "mean", "ממוצע", "ממוצעת")
_MAX_KEYWORDS = ("max", "maximum", "highest", "מקסימום", "הגבוה", "הכי גבוה")
_MIN_KEYWORDS = ("min", "minimum", "lowest", "מינימום", "הנמוך", "הכי נמוך")
_COUNT_KEYWORDS = ("count", "how many", "כמה", "מספר שורות")

_RATIO_KEYWORDS = ("per", "ratio", "יחס")

# "Is the denominator possibly zero?" hints a judge's `reason` string might
# contain -- used only by _mock_plan's extra_context handling, to make the
# judge -> replan loop genuinely exercisable in mock mode (see _mock_judge).
_ZERO_DENOMINATOR_HINTS = ("אפס", "zero", "inf", "nan")

_COLUMN_DTYPE_RE = re.compile(r"(\w+) \((\w+)\)")
_CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL)
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# thinking_level -> the reasoning_effort value sent to the router model.
# See module docstring for why this is a documented assumption, not a
# verified API contract of whatever model actually sits behind
# VLLM_ROUTER_URL.
_REASONING_EFFORT = {"fast": "low", "standard": "medium", "deep": "high"}


def _reasoning_effort(thinking_level: str) -> str:
    return _REASONING_EFFORT.get(thinking_level, "medium")


def generate_pandas_code(
    question: str,
    schema_description: str,
    previous_code: str | None = None,
    error: str | None = None,
) -> str:
    """Return a pandas code snippet (as a string) that computes an answer
    to ``question`` and stores it in a variable named ``result``.

    ``previous_code``/``error`` are supplied on a retry after a first
    attempt raised an exception in the sandbox -- real mode feeds them
    back to the LLM so it can self-correct; mock mode ignores them since
    its heuristic doesn't depend on prior attempts.

    Kept as a single-step convenience API (used only as ``generate_plan``'s
    fallback-of-last-resort); everyday callers should use ``generate_plan``.
    """
    if settings.MOCK_LLM:
        return _mock_generate(question, schema_description)
    return _real_generate(question, schema_description, previous_code, error, "standard")


def _first_numeric_column(schema_description: str) -> str | None:
    """Parse ``col (dtype)`` pairs out of describe_schema_text()'s output
    and return the first column name whose dtype looks numeric."""
    for match in _COLUMN_DTYPE_RE.finditer(schema_description):
        col, dtype = match.group(1), match.group(2).lower()
        if any(hint in dtype for hint in _NUMERIC_DTYPE_HINTS):
            return col
    return None


def _mock_generate(question: str, schema_description: str) -> str:
    q = question.lower()
    col = _first_numeric_column(schema_description)

    if col and any(kw in q for kw in _SUM_KEYWORDS):
        return f"result = df['{col}'].sum()"
    if col and any(kw in q for kw in _AVG_KEYWORDS):
        return f"result = df['{col}'].mean()"
    if col and any(kw in q for kw in _MAX_KEYWORDS):
        return f"result = df['{col}'].max()"
    if col and any(kw in q for kw in _MIN_KEYWORDS):
        return f"result = df['{col}'].min()"
    if any(kw in q for kw in _COUNT_KEYWORDS):
        return "result = len(df)"
    return "result = df.describe()"


def _build_prompt(question: str, schema_description: str, previous_code: str | None, error: str | None) -> str:
    base = (
        "אתה מומחה ל-pandas. בהינתן סכמה של DataFrame (משתנה בשם df, או "
        "מספר DataFrames במילון בשם dfs אם יש כמה גיליונות) וכן שאלה בשפה "
        "חופשית, החזר **רק** קטע קוד python בתוך בלוק ```python ... ``` "
        "שמחשב את התשובה ושומר אותה במשתנה בשם `result`. אל תשתמש ב-import, "
        "פתיחת קבצים, רשת, או כל קוד מעבר לחישוב עצמו על ה-DataFrame. אל "
        "תוסיף טקסט נוסף מעבר לבלוק הקוד.\n\n"
        f"סכמה:\n{schema_description}\n\nשאלה: {question}\n"
    )
    if previous_code and error:
        base += (
            f"\nניסיון קודם נכשל. הקוד שהורץ:\n```python\n{previous_code}\n```\n"
            f"השגיאה שהתקבלה: {error}\n"
            "תקן את הקוד בהתאם ושלח קטע קוד מתוקן בלבד, שוב בתוך בלוק ```python.\n"
        )
    return base


def _real_generate(
    question: str, schema_description: str, previous_code: str | None, error: str | None, thinking_level: str
) -> str:
    url = f"{settings.VLLM_ROUTER_URL.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.VLLM_ROUTER_MODEL,
        "messages": [
            {"role": "user", "content": _build_prompt(question, schema_description, previous_code, error)}
        ],
        "stream": False,
        "reasoning_effort": _reasoning_effort(thinking_level),
    }
    with httpx.Client(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()

    content = data["choices"][0]["message"]["content"]
    match = _CODE_BLOCK_RE.search(content)
    code = match.group(1).strip() if match else content.strip()
    if not code:
        logger.warning("Router returned empty/unparseable pandas code: %r", content)
    return code


# ---------------------------------------------------------------------------
# Multi-step plans
# ---------------------------------------------------------------------------


def _numeric_columns(schema_description: str) -> list[str]:
    """All column names in ``schema_description`` whose dtype looks numeric,
    in the order they appear (unlike ``_first_numeric_column``, which stops
    at the first one -- the ratio heuristic below needs at least two)."""
    return [
        match.group(1)
        for match in _COLUMN_DTYPE_RE.finditer(schema_description)
        if any(hint in match.group(2).lower() for hint in _NUMERIC_DTYPE_HINTS)
    ]


def generate_plan(
    question: str,
    schema_description: str,
    thinking_level: str = "standard",
    extra_context: str | None = None,
    system_prompt: str | None = None,
) -> list[dict[str, str]]:
    """Return an ordered list of ``{"description", "code"}`` steps (1-4)
    that together compute the answer to ``question``, with the final step
    assigning to ``result``. See module docstring for the mock/real split.

    ``extra_context``, when set, is a reason ``judge_result()`` gave for
    rejecting a *previous* plan's result -- folded into the prompt (real
    mode) / used as a targeted heuristic adjustment (mock mode, see
    ``_mock_plan``) so this replan actually has a chance of doing something
    different, not just regenerating the same plan.

    ``system_prompt`` is the conversation's custom instruction (see
    CONTRACTS.md "system_prompt") -- real mode only (mock mode's
    keyword-heuristic plan generation has no use for free-text instructions,
    see ``_mock_plan``).
    """
    if settings.MOCK_LLM:
        return _mock_plan(question, schema_description, extra_context)
    plan = _real_plan(question, schema_description, thinking_level, extra_context, system_prompt)
    if plan:
        return plan
    # Router unreachable or returned something unparseable as a plan --
    # fall back to the single-step generator wrapped as a 1-step plan
    # rather than failing the request outright.
    return [
        {
            "description": "חישוב ישיר (fallback)",
            "code": _real_generate(question, schema_description, None, None, thinking_level),
        }
    ]


def _mock_plan(question: str, schema_description: str, extra_context: str | None = None) -> list[dict[str, str]]:
    q = question.lower()
    numeric_cols = _numeric_columns(schema_description)
    mentioned = [c for c in numeric_cols if c.lower() in q]
    looks_like_ratio = any(kw in q for kw in _RATIO_KEYWORDS) or "/" in q

    if len(mentioned) >= 2 and looks_like_ratio:
        numerator, denominator = mentioned[0], mentioned[1]
        pick_min = any(kw in q for kw in _MIN_KEYWORDS) and not any(kw in q for kw in _MAX_KEYWORDS)

        steps: list[dict[str, str]] = []
        if extra_context and any(hint in extra_context.lower() for hint in _ZERO_DENOMINATOR_HINTS):
            # A prior attempt's judge flagged a NaN/Inf-looking result --
            # the classic cause for a ratio computation is a zero
            # denominator. Filter those rows out before computing the ratio
            # this time, so the replan is a genuine correction, not a
            # no-op repeat of the same heuristic.
            steps.append(
                {
                    "description": f"סינון שורות עם {denominator}=0 לפני חישוב היחס (בעקבות בדיקת תקינות)",
                    "code": f"df = df[df['{denominator}'] != 0]",
                }
            )
        steps.append(
            {
                "description": f"חישוב היחס {numerator}/{denominator} לכל שורה",
                "code": f"df['__ratio'] = df['{numerator}'] / df['{denominator}']",
            }
        )
        steps.append(
            {
                "description": "בחירת השורה עם היחס הנמוך ביותר" if pick_min else "בחירת השורה עם היחס הגבוה ביותר",
                "code": "result = df.loc[df['__ratio'].idxmin()]" if pick_min else "result = df.loc[df['__ratio'].idxmax()]",
            }
        )
        return steps

    # No clear multi-step pattern -- the original single-step heuristic
    # (sum/avg/max/min/count/describe) is still a correct 1-step plan.
    return [{"description": "חישוב ישיר על עמודה בודדת", "code": _mock_generate(question, schema_description)}]


def _build_plan_prompt(
    question: str, schema_description: str, extra_context: str | None, system_prompt: str | None = None
) -> str:
    base = (
        "אתה מומחה ל-pandas. בהינתן סכמה של DataFrame (משתנה בשם df, או "
        "מספר DataFrames במילון בשם dfs אם יש כמה גיליונות) וכן שאלה בשפה "
        "חופשית, פרק את הפתרון לרצף של 1 עד 4 שלבים קצרים. כל שלב יכול "
        "להשתמש במשתנים/עמודות שנוצרו בשלבים קודמים (למשל שלב 1 יוצר עמודה "
        "חדשה, שלב 2 ממיין או מסנן לפיה) -- אל תחזור על קוד של שלב קודם. "
        "**רק השלב האחרון** חייב לשמור את התשובה הסופית במשתנה בשם `result`. "
        "אם השאלה פשוטה וניתן לענות עליה בצעד אחד, החזר תוכנית של שלב אחד "
        "בלבד -- אל תמציא שלבים מיותרים.\n\n"
        'החזר **רק** JSON בפורמט: {"steps": [{"description": "...", "code": "..."}, ...]}. '
        "אל תשתמש ב-import, פתיחת קבצים, רשת, או כל קוד מעבר לחישוב עצמו על "
        "ה-DataFrame. אל תוסיף טקסט נוסף מעבר ל-JSON.\n\n"
        f"סכמה:\n{schema_description}\n\nשאלה: {question}\n"
    )
    if system_prompt:
        base += f"\nהנחיה כללית מהמשתמש לשיחה זו (קחה בחשבון בבניית התוכנית): {system_prompt}\n"
    if extra_context:
        base += (
            f"\nניסיון קודם הניב תוצאה שנפסלה בבדיקת תקינות. הסיבה שניתנה: "
            f"{extra_context}\nבנה תוכנית שמתייחסת לבעיה הזו (למשל: סנן "
            "נתונים בעייתיים לפני החישוב).\n"
        )
    return base


def _parse_steps(raw_steps: object, max_steps: int = 4) -> list[dict[str, str]] | None:
    if not isinstance(raw_steps, list) or not (1 <= len(raw_steps) <= max_steps):
        return None
    cleaned: list[dict[str, str]] = []
    for item in raw_steps:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        if not code:
            continue
        description = str(item.get("description", "")).strip() or "שלב"
        cleaned.append({"description": description, "code": code})
    return cleaned or None


def _real_plan(
    question: str,
    schema_description: str,
    thinking_level: str,
    extra_context: str | None,
    system_prompt: str | None = None,
) -> list[dict[str, str]] | None:
    url = f"{settings.VLLM_ROUTER_URL.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.VLLM_ROUTER_MODEL,
        "messages": [
            {
                "role": "user",
                "content": _build_plan_prompt(question, schema_description, extra_context, system_prompt),
            }
        ],
        "stream": False,
        "reasoning_effort": _reasoning_effort(thinking_level),
    }
    try:
        with httpx.Client(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        match = _JSON_BLOCK_RE.search(content)
        raw = match.group(1).strip() if match else content.strip()
        parsed = json.loads(raw)
        return _parse_steps(parsed.get("steps"))
    except Exception as exc:  # noqa: BLE001 -- any failure here just means "use the fallback"
        logger.warning("Router LLM plan generation failed (%s); falling back to single-step generate", exc)
        return None


def regenerate_step(
    question: str,
    schema_description: str,
    prior_steps: list[dict[str, str]],
    failed_step: dict[str, str],
    error: str,
    thinking_level: str = "standard",
) -> dict[str, str]:
    """Return a single corrected ``{"description", "code"}`` step to retry
    with, after ``failed_step`` raised ``error``. ``prior_steps`` are the
    steps that already ran successfully in this request (so a real-mode
    retry prompt can remind the model what's already in scope, without
    asking it to redo them).
    """
    if settings.MOCK_LLM:
        # The mock heuristic is deterministic -- regenerating the same
        # question produces the exact same (already-failing) code, so
        # there's nothing to "fix". Fall back to a step that always
        # succeeds rather than looping on a guaranteed repeat failure.
        return {"description": "נפילה חזרה ל-describe() לאחר כשל שלב", "code": "result = df.describe()"}
    step = _real_regenerate_step(question, schema_description, prior_steps, failed_step, error, thinking_level)
    if step:
        return step
    return {"description": "נפילה חזרה ל-describe() לאחר כשל בתיקון", "code": "result = df.describe()"}


def _build_step_retry_prompt(
    question: str,
    schema_description: str,
    prior_steps: list[dict[str, str]],
    failed_step: dict[str, str],
    error: str,
) -> str:
    prior_code = "\n\n".join(f"# {s['description']}\n{s['code']}" for s in prior_steps)
    return (
        "אתה ממשיך לפתור שאלה על DataFrame בעזרת pandas, כחלק מתוכנית "
        "רב-שלבית. השלבים הבאים כבר רצו בהצלחה באותו namespace -- "
        "המשתנים/העמודות שהם יצרו זמינים לך, אל תיצור אותם מחדש:\n"
        f"```python\n{prior_code or '# (אין שלבים קודמים שהצליחו)'}\n```\n\n"
        f"השלב הבא שניסית, '{failed_step['description']}', נכשל:\n"
        f"```python\n{failed_step['code']}\n```\n"
        f"שגיאה שהתקבלה: {error}\n\n"
        f"שאלה מקורית: {question}\nסכמה:\n{schema_description}\n\n"
        "החזר שלב מתוקן יחיד, JSON בלבד: "
        '{"description": "...", "code": "..."}. אם זהו השלב האחרון '
        "הנדרש בתוכנית, הקוד חייב לשמור את התשובה הסופית במשתנה `result`."
    )


def _real_regenerate_step(
    question: str,
    schema_description: str,
    prior_steps: list[dict[str, str]],
    failed_step: dict[str, str],
    error: str,
    thinking_level: str,
) -> dict[str, str] | None:
    url = f"{settings.VLLM_ROUTER_URL.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.VLLM_ROUTER_MODEL,
        "messages": [
            {
                "role": "user",
                "content": _build_step_retry_prompt(question, schema_description, prior_steps, failed_step, error),
            }
        ],
        "stream": False,
        "reasoning_effort": _reasoning_effort(thinking_level),
    }
    try:
        with httpx.Client(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        match = _JSON_BLOCK_RE.search(content)
        raw = match.group(1).strip() if match else content.strip()
        parsed = json.loads(raw)
        steps = _parse_steps([parsed], max_steps=1)
        return steps[0] if steps else None
    except Exception as exc:  # noqa: BLE001 -- any failure here just means "use the fallback"
        logger.warning("Router LLM step-retry generation failed (%s); falling back to describe()", exc)
        return None


# ---------------------------------------------------------------------------
# Judge (thinking_level == "deep" only)
# ---------------------------------------------------------------------------
#
# This is genuinely different from everything above: generate_plan/
# regenerate_step react to the sandbox *raising an exception* -- a clear,
# mechanical failure signal. The judge instead looks at a result that ran
# successfully without error and asks a semantic question an exception can
# never catch: "is this actually a believable answer to the question?" (a
# silent NaN/Inf from a zero-denominator ratio is the textbook example --
# the code runs fine, the number is nonsense). app/main.py only calls this
# when thinking_level=="deep", and only acts on it once (one judge call, at
# most one replan) -- see CONTRACTS.md "thinking_level" for why this is
# bounded rather than a loop.


def judge_result(
    question: str,
    schema_description: str,
    steps: list[dict[str, str]],
    result_display: object,
    thinking_level: str = "deep",
) -> dict[str, object]:
    """Return ``{"satisfactory": bool, "reason": str}`` -- whether
    ``result_display`` (the already-JSON-safe final value/table) looks like
    a believable answer to ``question``, given the schema and the steps
    that produced it.
    """
    if settings.MOCK_LLM:
        return _mock_judge(result_display)
    verdict = _real_judge(question, schema_description, steps, result_display, thinking_level)
    if verdict:
        return verdict
    return {"satisfactory": True, "reason": "שופט לא זמין (שגיאת רשת/פענוח); מתקבלת התוצאה כפי שהיא"}


def _is_bad_scalar(value: object) -> bool:
    return value is None or (
        isinstance(value, float) and (value != value or value in (float("inf"), float("-inf")))
    )


def _contains_bad_value(value: object) -> bool:
    """Recursively check for None/NaN/Inf anywhere in ``value`` -- a table
    result (dict for a single row/Series, list of dicts for a DataFrame) has
    the actual computed numbers *nested inside* it, not at the top level, so
    a top-level-only check would miss a NaN/Inf hiding in one field (exactly
    the zero-denominator-ratio case this judge exists to catch)."""
    if isinstance(value, dict):
        if not value:
            return True
        return any(_contains_bad_value(v) for v in value.values())
    if isinstance(value, list):
        if not value:
            return True
        return any(_contains_bad_value(v) for v in value)
    return _is_bad_scalar(value)


def _mock_judge(result_display: object) -> dict[str, object]:
    """Deterministic sanity check: None/NaN/Inf/empty anywhere in the result
    (top-level scalar, or nested inside a row/table) is never a believable
    answer, regardless of the question -- catches the classic
    zero-denominator-ratio failure mode without needing a real model.
    Anything else passes (the mock heuristic has no semantic understanding
    of "believable" beyond this).
    """
    if _contains_bad_value(result_display):
        return {
            "satisfactory": False,
            "reason": "התוצאה היא None/NaN/Inf/ריקה -- כנראה בעיה בחישוב (למשל חלוקה באפס)",
        }
    return {"satisfactory": True, "reason": "התוצאה נראית סבירה (לא None/NaN/Inf/ריקה)"}


def _build_judge_prompt(
    question: str, schema_description: str, steps: list[dict[str, str]], result_display: object
) -> str:
    steps_text = "\n\n".join(f"# {s['description']}\n{s['code']}" for s in steps)
    return (
        "אתה בודק איכות תשובה שחושבה על ידי קוד pandas. בהינתן השאלה, סכמת "
        "הנתונים, הקוד שרץ, והתוצאה הסופית -- שפוט אם זו תשובה סבירה "
        "ואמינה לשאלה (לא None/NaN/Inf, לא ריקה, לא סותרת את הסכמה, "
        "והגיונית ביחס למה שנשאל).\n\n"
        f"שאלה: {question}\nסכמה:\n{schema_description}\n\n"
        f"קוד שרץ:\n```python\n{steps_text}\n```\n\n"
        f"תוצאה סופית: {result_display!r}\n\n"
        'החזר **רק** JSON: {"satisfactory": true/false, "reason": "..."}.'
    )


def _real_judge(
    question: str,
    schema_description: str,
    steps: list[dict[str, str]],
    result_display: object,
    thinking_level: str,
) -> dict[str, object] | None:
    url = f"{settings.VLLM_ROUTER_URL.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.VLLM_ROUTER_MODEL,
        "messages": [
            {"role": "user", "content": _build_judge_prompt(question, schema_description, steps, result_display)}
        ],
        "stream": False,
        "reasoning_effort": _reasoning_effort(thinking_level),
    }
    try:
        with httpx.Client(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        match = _JSON_BLOCK_RE.search(content)
        raw = match.group(1).strip() if match else content.strip()
        parsed = json.loads(raw)
        if isinstance(parsed.get("satisfactory"), bool):
            return {
                "satisfactory": parsed["satisfactory"],
                "reason": str(parsed.get("reason", "")).strip() or "(לא צוינה סיבה)",
            }
    except Exception as exc:  # noqa: BLE001 -- any failure here just means "use the fallback"
        logger.warning("Router LLM judge call failed (%s); treating result as satisfactory", exc)
    return None
