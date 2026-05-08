"""
polling.py — הלולאה האסינכרונית שעוקבת אחרי interaction של Gemini.

עבור כל משימה פעילה רצה משימת asyncio נפרדת (asyncio.create_task)
שעושה polling כל POLL_INTERVAL_SEC שניות.

ה-polling מסתיים באחד מארבעה מצבים:
1. תוכנית מוכנה (פאזה planning הסתיימה) → שליחת התוכנית למשתמש
2. מחקר הסתיים (פאזה researching הסתיימה) → שליחת לינק לדו"ח
3. כשל מ-Gemini → דיווח למשתמש
4. המשימה בוטלה (הפאזה השתנתה ב-DB מבחוץ) → יציאה שקטה
"""

import asyncio
import logging
import os
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

import db
import gemini_client


logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 15
MAX_POLL_DURATION_SEC = 75 * 60  # 75 דקות; הדוק' אומרת מקסימום 60


def _build_base_url() -> str:
    """כתובת השרת שלנו. מוגדרת ב-env."""
    return os.environ.get("BASE_URL", "").rstrip("/")


# ----- שליחת התוכנית עם כפתורים -----
async def _send_plan_with_buttons(
    bot: Bot, chat_id: int, task_id: str, plan_text: str
) -> None:
    """
    שולח את התוכנית עם שני כפתורי inline:
    ✅ אשר את התוכנית | ✏️ בקש תיקון
    """
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ אשר את התוכנית", callback_data=f"a:{task_id}"
                ),
                InlineKeyboardButton(
                    "✏️ בקש תיקון", callback_data=f"r:{task_id}"
                ),
            ]
        ]
    )

    # טלגרם מגביל הודעה ל-4096 תווים. תוכניות בד"כ קצרות, אבל ליתר ביטחון:
    header = "📋 *תוכנית המחקר:*\n\n"
    body = plan_text
    max_body = 4096 - len(header) - 50  # מרווח ביטחון
    if len(body) > max_body:
        body = body[:max_body] + "\n\n_(התוכנית קוצרה)_"

    await bot.send_message(
        chat_id=chat_id,
        text=header + body,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


# ----- שליחת לינק לדו"ח הסופי -----
async def _send_report_link(
    bot: Bot, chat_id: int, task_id: str, title: str
) -> None:
    """שולח למשתמש לינק לדף ה-HTML של הדו"ח."""
    base_url = _build_base_url()
    report_url = f"{base_url}/r/{task_id}"
    archive_url = f"{base_url}/archive"

    text = (
        f"✅ *המחקר הושלם!*\n\n"
        f"📄 *{title}*\n\n"
        f"🔗 [צפייה בדו\"ח]({report_url})\n"
        f"📚 [כל המחקרים]({archive_url})"
    )
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


# ----- דיווח על כשל -----
async def _send_failure(bot: Bot, chat_id: int, error: str) -> None:
    """שולח למשתמש הודעת שגיאה ידידותית."""
    text = f"❌ המחקר נכשל\n\n`{error[:500]}`"
    try:
        await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Failed to send failure message: {e}")


# ----- הלולאה הראשית -----
async def watch_task(bot: Bot, task_id: str) -> None:
    """
    עוקב אחרי interaction של משימה.
    בכל ריצה: קורא את המשימה מ-DB, בודק מה הפאזה הנוכחית,
    מבצע polling ל-Gemini עד שהפאזה הזו מסתיימת, ועובר הלאה.

    הפונקציה מטפלת בשתי פאזות שדורשות polling:
    - planning → ממתינה לתוכנית, ואז מציגה אותה למשתמש
    - researching → ממתינה לדו"ח, ואז שולחת לינק

    בין הפאזות (awaiting_approval) הלולאה מסתיימת ומחכה
    שהמשתמש ילחץ כפתור — אז הקוד ב-main.py מפעיל אותה מחדש.
    """
    elapsed = 0
    last_known_interaction_id: Optional[str] = None

    while elapsed < MAX_POLL_DURATION_SEC:
        # קורא את המשימה מחדש בכל סיבוב — ככה אם המשתמש ביטל אותה
        # מבחוץ (שלח שאלה חדשה), נזהה את זה ונצא בנקיון.
        task = await db.get_task(task_id)
        if task is None:
            logger.warning(f"Task {task_id} disappeared from DB")
            return

        chat_id = task["chat_id"]
        phase = task["phase"]

        # פאזות סופיות — אנחנו לא צריכים לעקוב יותר
        if phase in (db.PHASE_COMPLETED, db.PHASE_FAILED):
            logger.info(f"Task {task_id} reached terminal phase: {phase}")
            return

        # פאזת המתנה לאישור — לא עושים polling, יוצאים ומחכים לכפתור
        if phase == db.PHASE_AWAITING_APPROVAL:
            logger.info(f"Task {task_id} awaiting user approval, exiting loop")
            return

        # פאזות פעילות שדורשות polling: planning / researching
        current_interaction_id = task["current_interaction_id"]

        # אם ה-interaction_id השתנה (למשל אחרי תיקון), מאפסים את הספירה
        if current_interaction_id != last_known_interaction_id:
            last_known_interaction_id = current_interaction_id
            elapsed = 0

        try:
            status = await gemini_client.check_status(current_interaction_id)
        except Exception as e:
            logger.exception(f"Error checking status for task {task_id}")
            await db.save_failed(task_id, f"API error: {e}")
            await _send_failure(bot, chat_id, str(e))
            return

        # Gemini עוד עובד — חכה ונסה שוב
        if not status["is_done"]:
            await asyncio.sleep(POLL_INTERVAL_SEC)
            elapsed += POLL_INTERVAL_SEC
            continue

        # Gemini נכשל
        if status["is_failed"]:
            error_msg = status["error"] or "Unknown error"
            await db.save_failed(task_id, error_msg)
            await _send_failure(bot, chat_id, error_msg)
            return

        # Gemini הצליח — עכשיו תלוי בפאזה מה לעשות עם הפלט
        output_text = status["output_text"] or ""

        if phase == db.PHASE_PLANNING:
            # התוכנית מוכנה
            await db.save_plan_ready(task_id, output_text)
            await _send_plan_with_buttons(bot, chat_id, task_id, output_text)
            return

        if phase == db.PHASE_RESEARCHING:
            # הדו"ח מוכן
            title = gemini_client.extract_title(output_text)
            await db.save_completed(task_id, output_text, title)
            await _send_report_link(bot, chat_id, task_id, title)
            return

        # פאזה לא צפויה — לוג ויציאה
        logger.error(f"Task {task_id} in unexpected phase: {phase}")
        return

    # יצאנו מהלולאה בגלל timeout
    logger.warning(f"Task {task_id} timed out after {MAX_POLL_DURATION_SEC}s")
    await db.save_failed(task_id, f"Timeout after {MAX_POLL_DURATION_SEC}s")
    task = await db.get_task(task_id)
    if task:
        await _send_failure(bot, task["chat_id"], "המחקר עבר את מגבלת הזמן")


def start_watching(bot: Bot, task_id: str) -> asyncio.Task:
    """
    מפעיל את watch_task ברקע.
    מחזיר את ה-asyncio.Task ליצור עליה reference (לא חובה לשמור).
    """
    return asyncio.create_task(watch_task(bot, task_id))
