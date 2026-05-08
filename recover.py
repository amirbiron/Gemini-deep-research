"""
recover.py — שחזור דו"ח שנשמר חלקית עקב באג בחילוץ טקסט.

שימוש:
    python recover.py <task_id>

מה זה עושה:
1. שולף את המשימה מ-MongoDB לפי ה-task_id.
2. קורא מחדש את ה-interaction מ-Gemini עם הקוד החדש.
3. אם ההרצה הצליחה ב-Gemini — חותך את רשימת המקורות
   ושומר חזרה את גוף הדו"ח ב-DB.

דורש את אותם משתני סביבה כמו השרת: MONGODB_URI, GEMINI_API_KEY.
מיועד להרצה ב-Render Shell (או מקומית עם הסביבה הנכונה).
"""

import asyncio
import sys

import db
import gemini_client


async def main(task_id: str) -> int:
    await db.init_db()
    try:
        task = await db.get_task(task_id)
        if task is None:
            print(f"[!] Task {task_id} not found")
            return 1

        interaction_id = task.get("current_interaction_id")
        if not interaction_id:
            print(f"[!] Task has no current_interaction_id")
            return 1

        print(f"[*] Task phase: {task.get('phase')}")
        print(f"[*] Re-fetching interaction {interaction_id} ...")

        status = await gemini_client.check_status(interaction_id)

        if not status["is_done"]:
            print("[!] Interaction is still in progress on Gemini's side")
            return 1
        if status["is_failed"]:
            print(f"[!] Interaction failed: {status['error']}")
            return 1

        full_text = status["output_text"] or ""
        print(f"[*] Raw output length: {len(full_text)} chars")

        body = gemini_client.strip_sources_section(full_text)
        print(f"[*] Body after sources strip: {len(body)} chars")

        if not body or body.startswith("["):
            print(f"[!] Extracted body looks empty/error: {body[:200]!r}")
            return 1

        title = gemini_client.extract_title(body)
        await db.save_completed(task_id, body, title)
        print(f"[+] Recovered. Title: {title}")
        print(f"[+] Saved {len(body)} chars to task {task_id}")
        return 0
    finally:
        await db.close_db()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python recover.py <task_id>")
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
