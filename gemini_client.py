"""
gemini_client.py — עטיפה אסינכרונית ל-Interactions API של Gemini
                   עם תמיכה ב-Deep Research + Collaborative Planning.

הקובץ הזה היחיד שיודע איך לדבר עם Gemini. שאר הקבצים קוראים
לפונקציות הציבוריות בלי להבין את המבנה הפנימי של ה-API.

הערה חשובה: ה-SDK של Gemini (google-genai) הוא סינכרוני.
כדי לא לחסום את ה-event loop של FastAPI, כל קריאה רצה דרך
asyncio.to_thread() — שמריץ את הפונקציה בת'רד נפרד.
"""

import asyncio
import os
import re
from typing import Optional, Any

from google import genai


# ----- הגדרות -----
AGENT_NAME = "deep-research-preview-04-2026"

# הוראת שפה — מתווספת לכל קלט כדי שגם התוכנית וגם הדו"ח יחזרו בעברית.
# מוגדרת באנגלית כי זה הכי אמין מול המודל, אבל מבקשת פלט בעברית.
LANGUAGE_INSTRUCTION = (
    "Please respond entirely in Hebrew (עברית). "
    "Both the research plan and the final report must be written in Hebrew. "
    "Keep proper nouns, brand names, code, and direct quotes from English "
    "sources in their original form."
)


def _with_language(text: str) -> str:
    """מוסיף הוראת שפה לכל קלט שיוצא ל-Gemini."""
    return f"{LANGUAGE_INSTRUCTION}\n\n---\n\n{text}"


# קונפיגורציית הסוכן — משותפת לכל הקריאות.
# collaborative_planning משתנה לפי השלב (תכנון/הרצה).
_BASE_AGENT_CONFIG = {
    "type": "deep-research",
    "thinking_summaries": "auto",
    "visualization": "auto",
}


# ----- מצב פנימי: client של Gemini -----
_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    """
    יוצר client בפעם הראשונה ושומר אותו ברמת המודול.
    Lazy init — לא מצריך init_db מקביל.

    מעביר את ה-API key במפורש ל-Client. ה-SDK של google-genai
    תומך בשני שמות סטנדרטיים: GEMINI_API_KEY ו-GOOGLE_API_KEY,
    אבל בחלק מהגרסאות הוא קורא רק את אחד מהם אוטומטית — לכן
    אנחנו קוראים את שניהם ידנית ומעבירים מפורש.
    """
    global _client
    if _client is None:
        api_key = (
            os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY (or GOOGLE_API_KEY) environment variable is not set"
            )
        _client = genai.Client(api_key=api_key)
    return _client


# ----- פעולות סינכרוניות פנימיות (ירוצו ב-thread) -----
def _sync_create_planning(query: str) -> str:
    """פותח interaction חדש לתכנון (collaborative_planning=True)."""
    interaction = _get_client().interactions.create(
        agent=AGENT_NAME,
        input=_with_language(query),
        agent_config={
            **_BASE_AGENT_CONFIG,
            "collaborative_planning": True,
        },
        background=True,
    )
    return interaction.id


def _sync_create_revision(previous_interaction_id: str, revision_text: str) -> str:
    """
    שולח הערות תיקון ל-Gemini, עם previous_interaction_id מצביע
    על interaction של תכנון קודם. נשארים בפאזת תכנון.
    """
    interaction = _get_client().interactions.create(
        agent=AGENT_NAME,
        input=_with_language(revision_text),
        agent_config={
            **_BASE_AGENT_CONFIG,
            "collaborative_planning": True,
        },
        previous_interaction_id=previous_interaction_id,
        background=True,
    )
    return interaction.id


def _sync_create_approval(previous_interaction_id: str) -> str:
    """
    מאשר את התוכנית האחרונה ומתחיל את המחקר עצמו.
    collaborative_planning=False = "תפסיק לתכנן, תרוץ".
    """
    interaction = _get_client().interactions.create(
        agent=AGENT_NAME,
        input=_with_language(
            "Plan looks good, please proceed with the research. "
            "Remember to write the final report in Hebrew."
        ),
        agent_config={
            **_BASE_AGENT_CONFIG,
            "collaborative_planning": False,
        },
        previous_interaction_id=previous_interaction_id,
        background=True,
    )
    return interaction.id


def _sync_get_interaction(interaction_id: str) -> Any:
    """שולף את המצב הנוכחי של interaction."""
    return _get_client().interactions.get(interaction_id)


# ----- פעולות ציבוריות אסינכרוניות -----
async def start_planning(query: str) -> str:
    """
    פותח שלב תכנון חדש. מחזיר interaction_id.
    הפלט יהיה תוכנית מחקר (לא הדו"ח עצמו).
    """
    return await asyncio.to_thread(_sync_create_planning, query)


async def revise_plan(
    previous_interaction_id: str, revision_text: str
) -> str:
    """
    שולח תיקון לתוכנית. מחזיר interaction_id חדש (לא אותו אחד!).
    """
    return await asyncio.to_thread(
        _sync_create_revision, previous_interaction_id, revision_text
    )


async def approve_and_research(previous_interaction_id: str) -> str:
    """
    מאשר את התוכנית ומתחיל את המחקר. מחזיר interaction_id חדש
    שמתייחס למחקר עצמו (לא לתכנון).
    """
    return await asyncio.to_thread(
        _sync_create_approval, previous_interaction_id
    )


async def check_status(interaction_id: str) -> dict[str, Any]:
    """
    בודק את הסטטוס של interaction (לא משנה אם זה תכנון או מחקר).
    מחזיר מילון אחיד:

        {
          "is_done": bool,           # True אם completed או failed
          "is_failed": bool,         # True רק אם failed
          "output_text": str | None, # רק אם הצליח (תוכנית או דו"ח)
          "error": str | None,       # רק אם נכשל
        }

    ההפרדה בין "תוכנית" ל"דו"ח" היא לפי הפאזה ב-DB, לא כאן —
    כי מבחינת Gemini שניהם אותו דבר: completed עם טקסט פלט.
    """
    interaction = await asyncio.to_thread(_sync_get_interaction, interaction_id)

    status = interaction.status

    if status == "completed":
        text = _extract_output_text(interaction)
        return {
            "is_done": True,
            "is_failed": False,
            "output_text": text,
            "error": None,
        }

    if status == "failed":
        # ה-error יכול להיות אובייקט או string — מנרמל
        error_msg = str(getattr(interaction, "error", "Unknown error"))
        return {
            "is_done": True,
            "is_failed": True,
            "output_text": None,
            "error": error_msg,
        }

    # in_progress (או כל סטטוס לא סופי אחר)
    return {
        "is_done": False,
        "is_failed": False,
        "output_text": None,
        "error": None,
    }


# ----- חילוץ טקסט מהפלט -----
def _text_from_content_list(content_list: Any) -> str:
    """מאחד את כל בלוקי ה-text מתוך רשימת content."""
    parts = []
    for item in content_list or []:
        item_type = getattr(item, "type", None)
        if item_type is None and isinstance(item, dict):
            item_type = item.get("type")
        if item_type != "text":
            continue
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text", "")
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


_SOURCES_HEADER_RE = re.compile(
    r"\n\s*(?:#{1,6}\s*)?\**\s*"
    r"(?:Sources|References|Citations|מקורות|הפניות|ביבליוגרפיה)"
    r"\s*:?\**\s*\n",
    re.IGNORECASE,
)


def strip_sources_section(text: str) -> str:
    """
    חותך כל מה שמופיע אחרי כותרת 'Sources' (או מקבילה בעברית/אנגלית) בסוף הדו"ח.
    שומר רק את גוף המחקר — בלי רשימת המקורות.

    החל רק על דו"ח סופי. אסור להחיל על תוכנית, כי תוכניות עשויות
    להכיל בלגיטימיות סעיף 'Sources to consult'.
    """
    if not text:
        return text
    match = _SOURCES_HEADER_RE.search("\n" + text)
    if match:
        cut = max(match.start() - 1, 0)
        return text[:cut].rstrip()
    return text


def _extract_output_text(interaction: Any) -> str:
    """
    מחלץ את הטקסט המלא של ה-interaction שהושלם — בלי לחתוך כלום.

    החיתוך של רשימת המקורות (strip_sources_section) הוא באחריות הקורא,
    כי הוא הפיכלי שיודע אם זו תוכנית (לא לחתוך) או דו"ח סופי (לחתוך).

    סדר העדיפויות:
    1. interaction.outputs — ה-API הרשמי לפלט הסופי (Interactions API).
    2. איחוד של כל ה-steps כ-fallback. _strip_sources_section יישא
       בנטל הסרת המקורות אם הקורא יבחר להפעיל אותה.
    """
    try:
        # 1. הדרך המועדפת: outputs
        outputs = getattr(interaction, "outputs", None)
        if outputs:
            text_parts = []
            for output in outputs:
                direct_text = getattr(output, "text", None)
                if direct_text:
                    text_parts.append(direct_text)
                    continue
                content = getattr(output, "content", None)
                if content:
                    combined = _text_from_content_list(content)
                    if combined:
                        text_parts.append(combined)
            joined = "\n\n".join(p for p in text_parts if p).strip()
            if joined:
                return joined

        # 2. fallback: איחוד כל הצעדים
        steps = getattr(interaction, "steps", None) or []
        all_parts = []
        for step in steps:
            content = getattr(step, "content", None)
            if content:
                text = _text_from_content_list(content)
                if text:
                    all_parts.append(text)
        joined = "\n\n".join(all_parts).strip()
        if joined:
            return joined

        return "[הדו\"ח הסתיים אך לא נמצא טקסט פלט]"
    except (AttributeError, IndexError, KeyError, TypeError) as e:
        return f"[שגיאה בחילוץ טקסט: {e}]"


# ----- כותרת לארכיון (לוגיקה מקומית, בלי קריאת API) -----
def extract_title(report_text: str, max_length: int = 80) -> str:
    """
    מחלץ כותרת קצרה מהדו"ח לתצוגה בארכיון.
    1. השורה הראשונה שמתחילה ב-# (כותרת Markdown)
    2. נופל ל-60-80 התווים הראשונים אם אין כותרת
    """
    if not report_text:
        return "ללא כותרת"

    for line in report_text.splitlines():
        stripped = line.strip()
        # שורה שמתחילה ב-# (כותרת Markdown), אחרי הסרת ה-#
        match = re.match(r"^#+\s+(.+)$", stripped)
        if match:
            title = match.group(1).strip()
            return title[:max_length] + ("…" if len(title) > max_length else "")

    # נפילה: 80 התווים הראשונים של הטקסט
    flat = " ".join(report_text.split())
    return flat[:max_length] + ("…" if len(flat) > max_length else "")
