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
def _extract_output_text(interaction: Any) -> str:
    """
    מחלץ את הטקסט הסופי מ-interaction שהושלם.
    לפי הדוקומנטציה: interaction.steps[-1].content[0].text.

    אנחנו עוברים על כל ה-content blocks ומאחדים את כל הטקסטים,
    כדי להיות עמידים למקרה שיש כמה blocks (text + image).
    תמונות מתעלמים מהן בגרסה הזו (אפשר להוסיף תמיכה בעתיד).
    """
    try:
        last_step = interaction.steps[-1]
        text_parts = []
        for content_item in last_step.content:
            # תמיכה הן ב-attribute access והן ב-dict access
            item_type = getattr(content_item, "type", None) or content_item.get("type")
            if item_type == "text":
                text = getattr(content_item, "text", None) or content_item.get("text", "")
                if text:
                    text_parts.append(text)
        return "\n\n".join(text_parts).strip()
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
