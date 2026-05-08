# Deep Research Bot

בוט טלגרם אישי שמריץ מחקרים עמוקים עם Gemini Deep Research Agent.
התוצאה — דו"ח HTML מעוצב + ארכיון אישי.

## תכונות

- 🧠 **Collaborative Planning** — אתה מאשר את תוכנית המחקר לפני ההרצה
- ✏️ אפשרות לתקן את התוכנית ללא הגבלה
- 🔍 מחקר אוטונומי (5–30 דקות)
- 📄 דו"ח HTML מעוצב עם RTL וכפתור העתקה
- 📚 ארכיון של כל המחקרים
- 🛑 משימה אחת בו-זמנית — שאלה חדשה מבטלת את הקודמת

## משתני סביבה

ראה `.env.example`.

## הפעלה לוקאלית

```bash
pip install -r requirements.txt
cp .env.example .env  # ערוך את הערכים
uvicorn main:app --reload --port 8000
```

ב-development צריך ngrok או Cloudflare Tunnel כדי שטלגרם
יוכל לפנות ל-webhook.

## פריסה ל-Render

1. דחוף את הקוד ל-GitHub
2. ב-Render: New → Blueprint → בחר את ה-repo
3. הגדר את משתני הסביבה בדשבורד (כל ה-`sync: false`)
4. אחרי הפריסה — עדכן את `BASE_URL` לכתובת הציבורית
5. ה-webhook מוגדר אוטומטית ב-startup

## מבנה

```
deep-research-bot/
├── main.py              # FastAPI + webhook + HTML routes
├── polling.py           # לולאת asyncio למעקב אחרי Gemini
├── gemini_client.py     # עטיפה ל-Interactions API
├── db.py                # MongoDB (motor)
├── templates/
│   ├── report.html
│   └── archive.html
├── requirements.txt
├── render.yaml
└── .env.example
```
