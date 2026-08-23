"""
Telegram Exam Monitor (deploy-friendly version)
-------------------------------------------------
Watches your Telegram channels/chats (including muted ones — mute is just a
notification setting, it does not affect API access) for messages from
teachers announcing exams, tries to figure out the exam date/time from
free-form text (Arabic or English), and pushes detected exams to a shared
jsonbin.io "bin" so your Study Tracker app can pick them up automatically.

This version reads all credentials from ENVIRONMENT VARIABLES instead of
hardcoding them in the file — required for deploying to a hosting platform
like Render, Railway, etc. without putting secrets in your code/repo.

REQUIRED ENVIRONMENT VARIABLES
-------------------------------
  TELEGRAM_API_ID          your api_id from https://my.telegram.org
  TELEGRAM_API_HASH        your api_hash from https://my.telegram.org
  JSONBIN_BIN_ID            your jsonbin.io bin id
  JSONBIN_MASTER_KEY        your jsonbin.io X-Master-Key

OPTIONAL ENVIRONMENT VARIABLES
-------------------------------
  TELEGRAM_WATCHED_CHATS    comma-separated chat names/usernames/ids to watch
                             (leave unset to watch ALL chats you're in)
  BACKFILL_DAYS              how many days of history to scan on startup
                             (default: 3)

TWO-PHASE SETUP (because Telegram login is interactive and most hosting
platforms can't handle that during a deploy)
-------------------------------------------------------------------------
PHASE 1 — do this once, on your own computer:
  1. pip install telethon dateparser requests
  2. Set the environment variables above (or just export them in your shell)
  3. Run:  python exam_monitor.py
  4. Log in with your phone number + the code Telegram sends you.
     This creates a file called "exam_monitor_session.session" in this
     folder — that file IS your logged-in session, so treat it like a
     password. Do not commit it to a public repo.
  5. Once you see "Exam monitor running...", you can Ctrl+C to stop it.

PHASE 2 — deploy it:
  1. Upload exam_monitor.py, requirements.txt, AND the
     "exam_monitor_session.session" file from Phase 1 to your host.
  2. On the hosting platform, set the same environment variables listed
     above (in its dashboard's "Environment" / "Secrets" section — not
     in the code).
  3. IMPORTANT: deploy this as a "Background Worker" / "long-running
     process" service type, not a "Web Service" — this script doesn't
     listen for HTTP requests, it just runs continuously in the
     background. If your host only offers web services, check whether
     it supports a worker/cron process type instead.
  4. Some free hosting tiers wipe the filesystem on every restart/deploy,
     which would delete the session file and force a fresh login each
     time. Check whether your host offers persistent storage/disk for
     this — if not, running it on your own always-on computer remains
     the simplest option for this particular script.
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta

from dateparser.search import search_dates
import requests
from telethon import TelegramClient, events

# ============ CONFIG — read from environment ============
def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"[fatal] missing required environment variable: {name}")
        sys.exit(1)
    return value


API_ID = int(_require_env("TELEGRAM_API_ID"))
API_HASH = _require_env("TELEGRAM_API_HASH")
JSONBIN_BIN_ID = _require_env("JSONBIN_BIN_ID")
JSONBIN_MASTER_KEY = _require_env("JSONBIN_MASTER_KEY")

SESSION_NAME = "exam_monitor_session"
JSONBIN_URL = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"

_watched_raw = os.environ.get("TELEGRAM_WATCHED_CHATS", "").strip()
WATCHED_CHATS = [c.strip() for c in _watched_raw.split(",") if c.strip()] if _watched_raw else []

BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "3"))
# ==========================================================

EXAM_KEYWORDS = [
    # English
    "exam", "quiz", "test", "midterm", "final", "assessment",
    "examination", "written test", "oral exam", "evaluation",
    # Arabic
    "امتحان", "الامتحان", "امتحانات", "اختبار", "الاختبار", "اختبارات",
    "فحص", "تقييم", "كويز", "مذاكرة", "نصفي", "نهائي", "استحقاقي",
]


def looks_like_exam_announcement(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in EXAM_KEYWORDS)


def extract_date(text: str):
    """Try to find a future-ish date (and time, if mentioned) in free-form text."""
    result = search_dates(
        text,
        languages=["ar", "en"],
        settings={
            "PREFER_DATES_FROM": "future",
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    if not result:
        return None, None
    matched_text, dt = result[0]
    date_str = dt.date().isoformat()
    has_time_pattern = bool(re.search(r'\d{1,2}\s*(:\d{2})?\s*(am|pm|ص|م)', matched_text, re.IGNORECASE))
    if has_time_pattern or (dt.hour != 0 or dt.minute != 0):
        time_str = dt.strftime("%H:%M")
    else:
        time_str = None
    return date_str, time_str


def guess_subject(text: str, chat_title: str) -> str:
    first_line = text.strip().split("\n")[0]
    if len(first_line) <= 60:
        return first_line
    return chat_title or "Exam"


def fetch_existing_exams():
    try:
        r = requests.get(
            JSONBIN_URL + "/latest",
            headers={"X-Master-Key": JSONBIN_MASTER_KEY},
            timeout=10,
        )
        r.raise_for_status()
        record = r.json().get("record", [])
        if isinstance(record, str):
            try:
                record = json.loads(record) if record.strip() else []
            except json.JSONDecodeError:
                record = []
        if not isinstance(record, list):
            record = []
        return record
    except Exception as e:
        print(f"[warn] could not fetch existing exams: {e}")
        return []


def push_exam(exam: dict):
    exams = fetch_existing_exams()
    exams = [e for e in exams if isinstance(e, dict)]
    if any(e.get("id") == exam["id"] for e in exams):
        return
    exams.append(exam)
    try:
        r = requests.put(
            JSONBIN_URL,
            json=exams,
            headers={
                "Content-Type": "application/json",
                "X-Master-Key": JSONBIN_MASTER_KEY,
            },
            timeout=10,
        )
        r.raise_for_status()
        when = f"{exam['date']} {exam['time']}" if exam.get('time') else exam['date']
        print(f"[ok] pushed exam: {exam['subject']} on {when}")
    except Exception as e:
        print(f"[error] failed to push exam: {e}")


client = TelegramClient(SESSION_NAME, API_ID, API_HASH)


async def backfill_recent_history():
    cutoff = datetime.now().astimezone() - timedelta(days=BACKFILL_DAYS)
    print(f"[backfill] scanning messages from the last {BACKFILL_DAYS} day(s)...")

    dialogs = await client.get_dialogs()
    for dialog in dialogs:
        if WATCHED_CHATS and dialog.name not in WATCHED_CHATS and str(dialog.id) not in WATCHED_CHATS:
            continue
        async for message in client.iter_messages(dialog.id, offset_date=None, limit=200):
            if message.date < cutoff:
                break
            text = message.raw_text or ""
            if not text or not looks_like_exam_announcement(text):
                continue
            exam_date, exam_time = extract_date(text)
            if not exam_date:
                continue
            exam = {
                "id": f"{message.chat_id}-{message.id}",
                "subject": guess_subject(text, dialog.name or "Exam"),
                "date": exam_date,
                "time": exam_time,
                "channel": dialog.name,
                "raw_text": text[:300],
                "detected_at": datetime.now().isoformat(),
            }
            push_exam(exam)
    print("[backfill] done.")


@client.on(events.NewMessage(chats=WATCHED_CHATS or None))
async def handler(event):
    text = event.raw_text or ""
    if not text or not looks_like_exam_announcement(text):
        return

    chat = await event.get_chat()
    chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", "") or "Unknown"

    exam_date, exam_time = extract_date(text)
    if not exam_date:
        print(f"[skip] exam keyword found but no date detected: {text[:80]!r}")
        return

    exam = {
        "id": f"{event.chat_id}-{event.id}",
        "subject": guess_subject(text, chat_title),
        "date": exam_date,
        "time": exam_time,
        "channel": chat_title,
        "raw_text": text[:300],
        "detected_at": datetime.now().isoformat(),
    }
    push_exam(exam)


async def main():
    await client.start()
    await backfill_recent_history()
    print("Exam monitor running. Watching:", WATCHED_CHATS or "ALL chats")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
