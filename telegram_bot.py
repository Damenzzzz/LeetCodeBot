"""
LeetCode Daily Checker Telegram Bot
Single-file Python bot using python-telegram-bot v20.

Features (baseline):
- /register <leetcode_nick>  - register your LeetCode nickname (stores Telegram user id)
- /unregister                - remove yourself
- /list                      - list registered users (admin only)
- /setgroup                  - set current chat as the report group (admin only)
- /check                     - manual check and post report to group
- daily scheduled check at configured time (Asia/Almaty timezone)

Added:
- Streaks: consecutive days with >=1 Accepted submission (calculated on daily report)
- Reminders: daily reminder message for users who haven't solved yet

Requires environment variable TELEGRAM_TOKEN with your bot token.

Optional environment variables:
- DAILY_HOUR / DAILY_MINUTE        (default 23:59 Asia/Almaty) - daily report time
- REMINDER_HOUR / REMINDER_MINUTE  (default 21:00 Asia/Almaty) - reminder time

Note: uses LeetCode public GraphQL endpoint to request recentSubmissionList(username: ...)
"""

import os
import sqlite3
from datetime import datetime, time, timedelta
import logging

import requests
from zoneinfo import ZoneInfo
from telegram import Update, ChatMember
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ----------------- Configuration -----------------
DB_PATH = "leetcode_bot.db"
LEETCODE_GRAPHQL = "https://leetcode.com/graphql"
TIMEZONE = ZoneInfo("Asia/Almaty")  # Kazakhstan time

# Daily report time (end-of-day check)
DAILY_HOUR = int(os.getenv("DAILY_HOUR", "23"))
DAILY_MINUTE = int(os.getenv("DAILY_MINUTE", "59"))

# Reminder time (ping those who haven't solved yet)
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "21"))
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "0"))

# --------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------- Database helpers -----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Users table (add columns for streak + last_solved_date)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            leetcode_nick TEXT,
            streak INTEGER DEFAULT 0,
            last_solved_date TEXT
        )
        """
    )

    # In case the DB was created before we added streak fields, try to migrate.
    # (SQLite doesn't support IF NOT EXISTS for ADD COLUMN reliably across versions)
    try:
        cur.execute("ALTER TABLE users ADD COLUMN streak INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE users ADD COLUMN last_solved_date TEXT")
    except sqlite3.OperationalError:
        pass

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def db_set_config(key, value):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("REPLACE INTO config(key,value) VALUES(?,?)", (key, value))
    conn.commit()
    conn.close()


def db_get_config(key):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value FROM config WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def add_user(telegram_id, username, leetcode_nick):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Preserve existing streak data if user re-registers
    cur.execute("SELECT streak, last_solved_date FROM users WHERE telegram_id=?", (telegram_id,))
    row = cur.fetchone()
    streak = row[0] if row else 0
    last_solved_date = row[1] if row else None

    cur.execute(
        """
        REPLACE INTO users(telegram_id, username, leetcode_nick, streak, last_solved_date)
        VALUES(?,?,?,?,?)
        """,
        (telegram_id, username, leetcode_nick, streak, last_solved_date),
    )
    conn.commit()
    conn.close()


def remove_user(telegram_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE telegram_id=?", (telegram_id,))
    conn.commit()
    conn.close()


def list_users():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT telegram_id, username, leetcode_nick, streak, last_solved_date FROM users")
    rows = cur.fetchall()
    conn.close()
    return rows


def update_user_streak(telegram_id: int, new_streak: int, last_solved_date: str | None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET streak=?, last_solved_date=? WHERE telegram_id=?",
        (new_streak, last_solved_date, telegram_id),
    )
    conn.commit()
    conn.close()


# ----------------- LeetCode helpers -----------------
def nick_escape(s: str) -> str:
    # basic escape for quotes
    return s.replace('"', '\\"')


def leetcode_recent_submissions(nick: str, limit: int = 20):
    # GraphQL query to get recentSubmissionList
    # LeetCode returns latest submissions first.
    q = (
        '{ recentSubmissionList(username: "%s") { title titleSlug timestamp statusDisplay lang } }'
        % nick_escape(nick)
    )
    resp = requests.post(LEETCODE_GRAPHQL, json={"query": q}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", {}).get("recentSubmissionList")


def solved_on_date(nick: str, target_date) -> tuple[bool | None, str | None]:
    """
    Returns (solved, err):
    - solved=True  if there is at least 1 Accepted submission on target_date
    - solved=False if no Accepted submission on target_date
    - solved=None  if request failed (err is filled)
    """
    try:
        subs = leetcode_recent_submissions(nick)
    except Exception as e:
        logger.exception("Error fetching submissions for %s: %s", nick, e)
        return None, str(e)

    if not subs:
        return False, None

    for item in subs:
        ts = item.get("timestamp")
        if ts is None:
            continue
        try:
            ts_int = int(ts)
        except Exception:
            try:
                ts_int = int(float(ts))
            except Exception:
                continue

        # Normalize timestamp (seconds vs ms)
        if ts_int > 1_000_000_000_000:
            ts_int //= 1000

        dt = datetime.fromtimestamp(ts_int, tz=TIMEZONE)
        if dt.date() == target_date and item.get("statusDisplay") == "Accepted":
            return True, None

    return False, None


def solved_today(nick: str) -> tuple[bool | None, str | None]:
    now = datetime.now(TIMEZONE).date()
    return solved_on_date(nick, now)



# --- Titles helper (Accepted tasks list) ---
def accepted_titles_on_date(nick: str, target_date) -> tuple[list[str] | None, str | None]:
    """
    Returns (titles, err):
    - titles: list of problem titles with Accepted submissions on target_date (unique, in order)
    - None if request failed (err is filled)
    """
    try:
        subs = leetcode_recent_submissions(nick)
    except Exception as e:
        logger.exception("Error fetching submissions for %s: %s", nick, e)
        return None, str(e)

    if not subs:
        return [], None

    titles: list[str] = []
    seen = set()

    for item in subs:
        ts = item.get("timestamp")
        if ts is None:
            continue
        try:
            ts_int = int(ts)
        except Exception:
            try:
                ts_int = int(float(ts))
            except Exception:
                continue

        if ts_int > 1_000_000_000_000:
            ts_int //= 1000

        dt = datetime.fromtimestamp(ts_int, tz=TIMEZONE)
        if dt.date() != target_date:
            continue

        if item.get("statusDisplay") != "Accepted":
            continue

        title = item.get("title") or item.get("titleSlug") or "Unknown"
        if title not in seen:
            seen.add(title)
            titles.append(title)

    return titles, None


def accepted_titles_today(nick: str) -> tuple[list[str] | None, str | None]:
    return accepted_titles_on_date(nick, datetime.now(TIMEZONE).date())

# ----------------- Telegram handlers -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот для проверки решения задач в LeetCode.\n"
        "Команды:\n"
        "• /register <ник_leetcode>\n"
        "• /unregister\n"
        "Админ-команды в группе:\n"
        "• /setgroup (куда слать отчёты)\n"
        "• /list\n"
        "• /check (ручная проверка)"
    )


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) != 1:
        await update.message.reply_text("Использование: /register <leetcode_nick>")
        return
    nick = context.args[0].strip()
    add_user(user.id, user.username or user.full_name, nick)
    await update.message.reply_text(f"Готово — зарегистрирован ник: {nick}")


async def unregister(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    remove_user(user.id)
    await update.message.reply_text("Ты удалён из списка участников.")


async def listcmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /list
    - без аргументов: показать список зарегистрированных (админ-only в группе)
    - с аргументом: /list @telegram_username  или /list <leetcode_nick>
        показывает названия задач, которые пользователь решил СЕГОДНЯ (Accepted).
    """
    chat = update.effective_chat
    user = update.effective_user

    # If user asked for tasks list for a specific person
    if len(context.args) == 1:
        target = context.args[0].strip()
        rows = list_users()

        # Resolve to leetcode nickname
        nick = None
        display = target

        if target.startswith("@"):
            # Match by stored username (might already include '@') or exact match.
            t = target.lower()
            for _, uname, lnick, _, _ in rows:
                if (uname or "").lower() == t or ("@" + (uname or "").lstrip("@")).lower() == t:
                    nick = lnick
                    display = uname
                    break
        else:
            # If not @, assume it's a LeetCode nickname directly
            nick = target

        if not nick:
            await update.message.reply_text("Не нашёл пользователя. Используй /list @username или /list <leetcode_nick>.")
            return

        titles, err = accepted_titles_today(nick)
        today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        if err is not None:
            await update.message.reply_text(f"Ошибка при проверке {display} ({nick}): {err}")
            return

        if not titles:
            await update.message.reply_text(f"{display} ({nick}) — сегодня ({today}) нет Accepted задач.")
            return

        msg = f"{display} ({nick}) — решённые сегодня ({today}) задачи:\n" + "\n".join([f"• {t}" for t in titles])
        await update.message.reply_text(msg)
        return

    # Default: admin-only list of registered users (when in group)
    if chat.type in ("group", "supergroup"):
        member = await chat.get_member(user.id)
        if member.status not in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
            await update.message.reply_text("Только админы могут просматривать список участников. Для задач используй /list @username.")
            return

    rows = list_users()
    if not rows:
        await update.message.reply_text("Список пуст.")
        return

    text_out = "Зарегистрированные пользователи:\n"
    for _, uname, nick, streak, last_date in rows:
        last_part = f", last: {last_date}" if last_date else ""
        text_out += f"- {uname} ({nick}) — streak: {streak}{last_part}\n"
    await update.message.reply_text(text_out)

async def setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Эту команду нужно вызывать в группе, где бот будет публиковать отчёты.")
        return

    # check admin
    member = await chat.get_member(user.id)
    if member.status not in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
        await update.message.reply_text("Только админ группы может установить эту группу как отчетную.")
        return

    db_set_config("report_chat_id", str(chat.id))
    await update.message.reply_text("Эта группа установлена как отчетная. Ежедневные отчёты будут отправляться сюда.")


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Проверяю...")
    await run_check_and_report(context, update_streaks=False)
    await update.message.reply_text("Готово.")


# ----------------- Core check + report -----------------
def _format_user_mention(username: str | None, display_name: str) -> str:
    # If telegram username exists, we can @mention; otherwise show name.
    if username and username.strip() and " " not in username:
        if username.startswith("@"):
            return username
        return f"@{username}"
    return display_name


async def run_check_and_report(context: ContextTypes.DEFAULT_TYPE, update_streaks: bool):
    """
    If update_streaks=True, we update streak fields based on today's result.
    Use update_streaks=True for the scheduled end-of-day report.
    """
    chat_id = db_get_config("report_chat_id")
    if not chat_id:
        logger.warning("Report chat id not set")
        return
    chat_id = int(chat_id)

    rows = list_users()
    if not rows:
        await context.bot.send_message(chat_id=chat_id, text="Нет зарегистрированных участников.")
        return

    today = datetime.now(TIMEZONE).date()
    yesterday = today - timedelta(days=1)

    report_lines = []
    for tid, uname, nick, streak, last_date in rows:
        ok, err = solved_on_date(nick, today)

        if err is not None:
            line = f"{uname} ({nick}): ❓ ошибка при проверке: {err}"
            report_lines.append(line)
            continue

        if ok:
            # Calculate new streak only during scheduled daily report
            new_streak = streak
            new_last_date = last_date
            if update_streaks:
                if last_date == str(yesterday):
                    new_streak = int(streak or 0) + 1
                else:
                    new_streak = 1
                new_last_date = str(today)
                update_user_streak(tid, new_streak, new_last_date)

            line = f"{uname} ({nick}): ✅ решал сегодня — streak: {new_streak if update_streaks else streak}"
        else:
            # If missed today, streak resets at end-of-day report
            if update_streaks:
                update_user_streak(tid, 0, last_date)
            line = f"{uname} ({nick}): ❌ не решал — streak: {0 if update_streaks else streak}"

        report_lines.append(line)

    header = f"Ежедневный отчёт — {today.strftime('%Y-%m-%d')}"
    full = header + "\n\n" + "\n".join(report_lines)
    await context.bot.send_message(chat_id=chat_id, text=full)


async def run_reminder(context: ContextTypes.DEFAULT_TYPE):
    """
    Reminder message: ping those who have NOT solved yet today.
    Does NOT update streaks (streaks are updated in end-of-day report).
    """
    chat_id = db_get_config("report_chat_id")
    if not chat_id:
        logger.warning("Report chat id not set")
        return
    chat_id = int(chat_id)

    rows = list_users()
    if not rows:
        return

    today = datetime.now(TIMEZONE).date()
    need_ping = []

    # We try to mention users by their Telegram username stored in DB (we keep it in 'username' field).
    # In your current DB, 'username' might be either @username or full name; that's OK.
    for _, uname, nick, _, _ in rows:
        ok, err = solved_on_date(nick, today)
        if err is not None:
            # Don't spam reminder for API errors; just skip
            continue
        if not ok:
            need_ping.append(uname)

    if not need_ping:
        await context.bot.send_message(chat_id=chat_id, text="✅ Напоминание: сегодня уже все решили минимум 1 задачу!")
        return

    mentions = ", ".join([_format_user_mention(u, u) for u in need_ping])
    msg = (
        f"⏰ Напоминание! Сегодня нужно решить минимум 1 задачу на LeetCode.\n"
        f"Кто ещё не решил: {mentions}"
    )
    await context.bot.send_message(chat_id=chat_id, text=msg)


# ----------------- Main -----------------
def main():
    init_db()
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        print("ERROR: set TELEGRAM_TOKEN environment variable")
        return

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("unregister", unregister))
    app.add_handler(CommandHandler("list", listcmd))
    app.add_handler(CommandHandler("setgroup", setgroup))
    app.add_handler(CommandHandler("check", check_command))

    # Scheduled jobs
    async def daily_report_job(context: ContextTypes.DEFAULT_TYPE):
        await run_check_and_report(context, update_streaks=True)

    async def daily_reminder_job(context: ContextTypes.DEFAULT_TYPE):
        await run_reminder(context)

    # Run daily reminder and daily report in Asia/Almaty timezone
    app.job_queue.run_daily(
        daily_reminder_job,
        time=time(hour=REMINDER_HOUR, minute=REMINDER_MINUTE, tzinfo=TIMEZONE),
    )
    app.job_queue.run_daily(
        daily_report_job,
        time=time(hour=DAILY_HOUR, minute=DAILY_MINUTE, tzinfo=TIMEZONE),
    )

    print("Bot started. Press Ctrl-C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()