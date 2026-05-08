"""
db.py — שכבת גישה ל-MongoDB עבור deep-research-bot.

כל פעולה על המסד עוברת דרך הקובץ הזה. אף קובץ אחר לא מדבר ישירות
עם motor/pymongo.

מחזור החיים של משימה:
    planning → awaiting_approval → researching → completed
                       ↓
                       └→ planning (אם ביקשת תיקון)
"""

import os
from datetime import datetime, timezone
from typing import Optional, Any

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection


# ----- מצב פנימי ברמת המודול -----
_client: Optional[AsyncIOMotorClient] = None
_collection: Optional[AsyncIOMotorCollection] = None

DB_NAME = "deep_research_bot"
COLLECTION_NAME = "research_tasks"

# ----- פאזות אפשריות (קבועים למניעת typos) -----
PHASE_PLANNING = "planning"
PHASE_AWAITING_APPROVAL = "awaiting_approval"
PHASE_RESEARCHING = "researching"
PHASE_COMPLETED = "completed"
PHASE_FAILED = "failed"

# פאזות שבהן המשימה עדיין "חיה" (אפשר לבטל/להמשיך)
ACTIVE_PHASES = (PHASE_PLANNING, PHASE_AWAITING_APPROVAL, PHASE_RESEARCHING)


# ----- אתחול -----
async def init_db() -> None:
    """פותח חיבור ל-MongoDB. נקרא פעם אחת מ-FastAPI lifespan."""
    global _client, _collection

    mongo_uri = os.environ["MONGODB_URI"]
    _client = AsyncIOMotorClient(mongo_uri)
    _collection = _client[DB_NAME][COLLECTION_NAME]

    await _collection.create_index([("created_at", -1)])
    await _collection.create_index("phase")
    await _collection.create_index([("chat_id", 1), ("phase", 1)])


async def close_db() -> None:
    """סוגר את החיבור. נקרא ב-shutdown של FastAPI."""
    global _client
    if _client is not None:
        _client.close()
        _client = None


def _coll() -> AsyncIOMotorCollection:
    if _collection is None:
        raise RuntimeError("DB not initialized. Call init_db() first.")
    return _collection


# ----- כלי עזר -----
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_str_id(oid: ObjectId) -> str:
    return str(oid)


def _to_object_id(task_id: str) -> Optional[ObjectId]:
    """str → ObjectId. None אם המחרוזת שבורה."""
    try:
        return ObjectId(task_id)
    except (InvalidId, TypeError):
        return None


# ----- פעולות ציבוריות: יצירה -----
async def create_task(
    chat_id: int,
    query: str,
    initial_interaction_id: str,
) -> str:
    """יוצר משימה חדשה בפאזת planning."""
    doc = {
        "chat_id": chat_id,
        "query": query,
        "current_interaction_id": initial_interaction_id,
        "phase": PHASE_PLANNING,
        "plan_text": None,
        "report_text": None,
        "report_title": None,
        "awaiting_revision_input": False,
        "error": None,
        "created_at": _now(),
        "completed_at": None,
    }
    result = await _coll().insert_one(doc)
    return _to_str_id(result.inserted_id)


# ----- פעולות ציבוריות: קריאה -----
async def get_task(task_id: str) -> Optional[dict[str, Any]]:
    """שולף משימה. None אם לא קיימת או id שבור."""
    oid = _to_object_id(task_id)
    if oid is None:
        return None
    return await _coll().find_one({"_id": oid})


async def get_active_task(chat_id: int) -> Optional[dict[str, Any]]:
    """
    שולף משימה פעילה (לא הסתיימה) של chat_id.
    שימוש: לפני יצירת משימה חדשה — לבטל את הישנה אם קיימת.
    """
    return await _coll().find_one(
        {"chat_id": chat_id, "phase": {"$in": list(ACTIVE_PHASES)}},
        sort=[("created_at", -1)],
    )


async def get_task_awaiting_revision(
    chat_id: int,
) -> Optional[dict[str, Any]]:
    """
    שולף משימה שמחכה לטקסט תיקון מהמשתמש.
    שימוש: כשמתקבלת הודעה — להבין אם זו שאלה חדשה או תיקון.
    """
    return await _coll().find_one(
        {"chat_id": chat_id, "awaiting_revision_input": True},
        sort=[("created_at", -1)],
    )


async def list_completed_tasks(limit: int = 50) -> list[dict[str, Any]]:
    """משימות שהושלמו, מהחדש לישן. לארכיון."""
    cursor = (
        _coll()
        .find({"phase": PHASE_COMPLETED})
        .sort("created_at", -1)
        .limit(limit)
    )
    return await cursor.to_list(length=limit)


# ----- פעולות ציבוריות: מעברי פאזה -----
async def save_plan_ready(task_id: str, plan_text: str) -> None:
    """תוכנית מוכנה. עוברים ל-awaiting_approval."""
    oid = _to_object_id(task_id)
    if oid is None:
        return
    await _coll().update_one(
        {"_id": oid},
        {
            "$set": {
                "phase": PHASE_AWAITING_APPROVAL,
                "plan_text": plan_text,
                "awaiting_revision_input": False,
            }
        },
    )


async def start_revision(task_id: str) -> None:
    """המשתמש לחץ 'תקן'. מסמן שמחכים לטקסט תיקון."""
    oid = _to_object_id(task_id)
    if oid is None:
        return
    await _coll().update_one(
        {"_id": oid},
        {"$set": {"awaiting_revision_input": True}},
    )


async def submit_revision(task_id: str, new_interaction_id: str) -> None:
    """המשתמש שלח טקסט תיקון. interaction חדש, חזרה ל-planning."""
    oid = _to_object_id(task_id)
    if oid is None:
        return
    await _coll().update_one(
        {"_id": oid},
        {
            "$set": {
                "phase": PHASE_PLANNING,
                "current_interaction_id": new_interaction_id,
                "awaiting_revision_input": False,
                "plan_text": None,
            }
        },
    )


async def approve_and_start_research(
    task_id: str, new_interaction_id: str
) -> None:
    """המשתמש אישר. interaction חדש, מעבר ל-researching."""
    oid = _to_object_id(task_id)
    if oid is None:
        return
    await _coll().update_one(
        {"_id": oid},
        {
            "$set": {
                "phase": PHASE_RESEARCHING,
                "current_interaction_id": new_interaction_id,
                "awaiting_revision_input": False,
            }
        },
    )


async def save_completed(
    task_id: str,
    report_text: str,
    report_title: str,
) -> None:
    """סוגר כ-completed עם הדו"ח."""
    oid = _to_object_id(task_id)
    if oid is None:
        return
    await _coll().update_one(
        {"_id": oid},
        {
            "$set": {
                "phase": PHASE_COMPLETED,
                "report_text": report_text,
                "report_title": report_title,
                "completed_at": _now(),
            }
        },
    )


async def save_failed(task_id: str, error: str) -> None:
    """סוגר כ-failed עם הודעת שגיאה."""
    oid = _to_object_id(task_id)
    if oid is None:
        return
    await _coll().update_one(
        {"_id": oid},
        {
            "$set": {
                "phase": PHASE_FAILED,
                "error": error,
                "completed_at": _now(),
            }
        },
    )
