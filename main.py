"""
main.py — נקודת הכניסה היחידה לאפליקציה.

מכילה:
1. FastAPI app + lifespan (init_db / close_db)
2. Telegram webhook handler — מקבל הודעות וכפתורים
3. routes ל-HTML — /r/{task_id} (דו"ח בודד) ו-/archive (רשימה)

הבוט מוגדר ב-startup דרך setWebhook אוטומטי (אם BASE_URL מוגדר).
לחלופין אפשר להגדיר ידנית פעם אחת מול ה-API של טלגרם.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import markdown as md
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from telegram import Bot, Update
from telegram.constants import ParseMode

import db
import gemini_client
import polling


# ----- לוגינג -----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ----- הגדרות מ-env -----
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")


# ----- אובייקט הבוט (singleton) -----
_bot: Optional[Bot] = None


def get_bot() -> Bot:
    """מחזיר את ה-Bot, יוצר אותו pעם הראשונה."""
    global _bot
    if _bot is None:
        if not TELEGRAM_BOT_TOKEN:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        _bot = Bot(token=TELEGRAM_BOT_TOKEN)
    return _bot


# ----- Lifespan: init/shutdown -----
@asynccontextmanager
async def lifespan(app: FastAPI):
    """אתחול וסגירה של משאבים."""
    logger.info("Starting up: connecting to DB...")
    await db.init_db()

    # הגדרת webhook אוטומטית אם יש BASE_URL ו-SECRET
    if BASE_URL and WEBHOOK_SECRET:
        webhook_url = f"{BASE_URL}/telegram/{WEBHOOK_SECRET}"
        try:
            await get_bot().set_webhook(url=webhook_url)
            logger.info(f"Webhook set to {webhook_url}")
        except Exception as e:
            logger.error(f"Failed to set webhook: {e}")
    else:
        logger.warning("BASE_URL or WEBHOOK_SECRET missing — webhook not set")

    yield

    logger.info("Shutting down: closing DB...")
    await db.close_db()


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


# ----- אימות שולח -----
def _is_authorized_chat(chat_id: int) -> bool:
    """רק ה-chat_id המוגדר יכול להשתמש בבוט."""
    return TELEGRAM_CHAT_ID != 0 and chat_id == TELEGRAM_CHAT_ID


# ----- הזרימה: שאלה חדשה -----
async def _handle_new_query(chat_id: int, query: str) -> None:
    """
    משתמש שלח שאלה חדשה. אם יש משימה פעילה — מבטלים אותה.
    פותחים משימה חדשה ומתחילים לעקוב.
    """
    bot = get_bot()

    # ביטול משימה פעילה אם קיימת
    active = await db.get_active_task(chat_id)
    if active:
        await db.save_failed(
            str(active["_id"]), "Cancelled by user (new query started)"
        )
        await bot.send_message(
            chat_id=chat_id, text="🛑 בוטל המחקר הקודם, מתחיל חדש"
        )

    # פותחים interaction חדש לתכנון
    try:
        interaction_id = await gemini_client.start_planning(query)
    except Exception as e:
        logger.exception("Failed to start planning")
        await bot.send_message(
            chat_id=chat_id, text=f"❌ שגיאה בפתיחת המחקר:\n`{e}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # יוצרים משימה ב-DB
    task_id = await db.create_task(chat_id, query, interaction_id)
    await bot.send_message(chat_id=chat_id, text="🧠 מתכנן מחקר...")

    # מתחילים polling ברקע
    polling.start_watching(bot, task_id)


# ----- הזרימה: טקסט תיקון -----
async def _handle_revision_text(
    task_id: str, revision_text: str
) -> None:
    """המשתמש שלח טקסט תיקון לתוכנית קיימת."""
    bot = get_bot()
    task = await db.get_task(task_id)
    if task is None:
        return
    chat_id = task["chat_id"]

    try:
        new_interaction_id = await gemini_client.revise_plan(
            task["current_interaction_id"], revision_text
        )
    except Exception as e:
        logger.exception("Failed to revise plan")
        await bot.send_message(
            chat_id=chat_id, text=f"❌ שגיאה בשליחת התיקון:\n`{e}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await db.submit_revision(task_id, new_interaction_id)
    await bot.send_message(chat_id=chat_id, text="✏️ מעדכן את התוכנית...")
    polling.start_watching(bot, task_id)


# ----- הזרימה: לחיצה על כפתור -----
async def _handle_callback(
    callback_id: str, chat_id: int, action: str, task_id: str
) -> None:
    """משתמש לחץ על אחד הכפתורים: a (אשר) או r (תקן)."""
    bot = get_bot()
    task = await db.get_task(task_id)

    if task is None:
        await bot.answer_callback_query(callback_id, text="המשימה לא נמצאה")
        return

    if task["phase"] != db.PHASE_AWAITING_APPROVAL:
        await bot.answer_callback_query(
            callback_id, text="המשימה כבר לא בשלב אישור"
        )
        return

    if action == "a":
        # אישור התוכנית
        await bot.answer_callback_query(callback_id, text="✅ אושר")
        try:
            new_interaction_id = await gemini_client.approve_and_research(
                task["current_interaction_id"]
            )
        except Exception as e:
            logger.exception("Failed to approve")
            await bot.send_message(
                chat_id=chat_id, text=f"❌ שגיאה: `{e}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await db.approve_and_start_research(task_id, new_interaction_id)
        await bot.send_message(
            chat_id=chat_id,
            text="🔍 מתחיל מחקר... (זה יכול לקחת 5-30 דקות)",
        )
        polling.start_watching(bot, task_id)

    elif action == "r":
        # בקשת תיקון
        await bot.answer_callback_query(callback_id, text="✏️ ממתין לתיקון")
        await db.start_revision(task_id)
        await bot.send_message(
            chat_id=chat_id,
            text="✏️ שלח את ההערות לתיקון בהודעה הבאה",
        )


# ----- Telegram webhook endpoint -----
@app.post("/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request):
    """
    נקודת כניסה ל-updates מטלגרם.
    הסוד בנתיב הוא הגנה בסיסית — רק טלגרם יודע אותו.
    """
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    update = Update.de_json(data, get_bot())

    # ----- callback query (לחיצת כפתור) -----
    if update.callback_query:
        cb = update.callback_query
        chat_id = cb.message.chat.id if cb.message else 0
        if not _is_authorized_chat(chat_id):
            return {"ok": True}

        callback_data = cb.data or ""
        if ":" in callback_data:
            action, task_id = callback_data.split(":", 1)
            await _handle_callback(cb.id, chat_id, action, task_id)
        return {"ok": True}

    # ----- הודעה רגילה -----
    if update.message and update.message.text:
        msg = update.message
        chat_id = msg.chat.id
        text = msg.text.strip()

        if not _is_authorized_chat(chat_id):
            return {"ok": True}

        # פקודות בסיסיות
        if text.startswith("/start") or text.startswith("/help"):
            await get_bot().send_message(
                chat_id=chat_id,
                text=(
                    "👋 שלח שאלת מחקר ואני אתכנן ואחקור.\n\n"
                    "🔹 אקבל תוכנית לאישור לפני המחקר\n"
                    f"🔹 תוכל לראות מחקרים קודמים ב-{BASE_URL}/archive"
                ),
            )
            return {"ok": True}

        # האם זה טקסט תיקון לתוכנית קיימת?
        awaiting = await db.get_task_awaiting_revision(chat_id)
        if awaiting:
            await _handle_revision_text(str(awaiting["_id"]), text)
            return {"ok": True}

        # שאלה חדשה
        await _handle_new_query(chat_id, text)
        return {"ok": True}

    return {"ok": True}


# ----- Health check -----
@app.get("/", response_class=PlainTextResponse)
async def health():
    return "OK"


# ----- HTML routes -----
def _markdown_to_html(text: str) -> str:
    """המרת Markdown ל-HTML עם הרחבות שימושיות."""
    return md.markdown(
        text,
        extensions=["fenced_code", "tables", "nl2br", "toc"],
    )


@app.get("/r/{task_id}", response_class=HTMLResponse)
async def view_report(request: Request, task_id: str):
    """דף תצוגה לדו"ח בודד."""
    task = await db.get_task(task_id)
    if task is None or task["phase"] != db.PHASE_COMPLETED:
        raise HTTPException(status_code=404, detail="Report not found")

    return templates.TemplateResponse(
        "report.html",
        {
            "request": request,
            "title": task.get("report_title") or "מחקר",
            "query": task.get("query", ""),
            "report_html": _markdown_to_html(task.get("report_text") or ""),
            "report_raw": task.get("report_text") or "",
            "created_at": task.get("created_at"),
        },
    )


@app.get("/archive", response_class=HTMLResponse)
async def view_archive(request: Request):
    """דף ארכיון של כל המחקרים."""
    tasks = await db.list_completed_tasks(limit=100)
    items = [
        {
            "id": str(t["_id"]),
            "title": t.get("report_title") or "ללא כותרת",
            "query": t.get("query", ""),
            "created_at": t.get("created_at"),
        }
        for t in tasks
    ]
    return templates.TemplateResponse(
        "archive.html", {"request": request, "items": items}
    )
