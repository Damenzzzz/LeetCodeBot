"""
LeetCode Group Checker Bot (python-telegram-bot v20)

Что умеет (по ТЗ):
- /register <leetcode_nick>   — регистрируешь ник
- /unregister                 — удаляешься
- /setgroup                   — (админ) назначить чат для напоминаний/отчётов
- /list                       — ДЛЯ ВСЕХ: показывает у каждого кол-во решённых задач сегодня + ✅/❌ (решил ≥1 или нет)
- /list @user                 — ДЛЯ ВСЕХ: показывает названия задач, решённых этим пользователем сегодня
- /check                      — ДЛЯ ВСЕХ: показывает твои задачи за сегодня (сколько и какие)
- /week                       — статистика за последние 7 дней (итоги по каждому)
- /week @user                 — статистика за 7 дней для конкретного пользователя

Авто-уведомления:
- каждые 3 часа: пингует тех, кто сегодня ещё не решил ни 1 задачу (с упоминаниями)
- как только ВСЕ решили ≥1 задачу (проверяется в цикле напоминаний): пишет поздравление 1 раз в день
- ежедневный отчёт в конце дня: показывает итоговый статус + MVP дня (кто решил больше всех) и сохраняет статистику в БД

Важно:
- streak полностью убран
- бот использует LeetCode GraphQL recentSubmissionList

Env:
- TELEGRAM_TOKEN (обязательно)
- DAILY_HOUR / DAILY_MINUTE (по умолчанию 23:59 Asia/Almaty)
"""

import os
import json
import sqlite3
import logging
from datetime import datetime, date, timedelta, time
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, List, Dict

import requests
from telegram import Update, ChatMember
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ----------------- Config -----------------
DB_PATH = "leetcode_bot.db"
LEETCODE_GRAPHQL = "https://leetcode.com/graphql"
TZ = ZoneInfo("Asia/Almaty")

DAILY_HOUR = int(os.getenv("DAILY_HOUR", "23"))
DAILY_MINUTE = int(os.getenv("DAILY_MINUTE", "59"))

REMINDER_INTERVAL_SECONDS = 3 * 60 * 60  # 3 hours
CACHE_TTL_SECONDS = 120  # 2 minutes, to avoid repeated API calls spam

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# in-memory cache: (nick, yyyy-mm-dd) -> (titles_list, fetched_at_epoch_seconds)
_cache: Dict[Tuple[str, str], Tuple[List[str], float]] = {}


# ----------------- DB helpers -----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # users: keep minimal columns; if older table has more columns, it's fine.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            leetcode_nick TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

    # daily statistics snapshots
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_stats (
            day TEXT,
            telegram_id INTEGER,
            solved_count INTEGER,
            titles_json TEXT,
            PRIMARY KEY(day, telegram_id)
        )
        """
    )

    conn.commit()
    conn.close()


def db_set_config(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("REPLACE INTO config(key,value) VALUES(?,?)", (key, value))
    conn.commit()
    conn.close()


def db_get_config(key: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value FROM config WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def add_user(tid: int, username: str, nick: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "REPLACE INTO users(telegram_id, username, leetcode_nick) VALUES(?,?,?)",
        (tid, username, nick),
    )
    conn.commit()
    conn.close()


def remove_user(tid: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE telegram_id=?", (tid,))
    conn.commit()
    conn.close()


def list_users():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT telegram_id, username, leetcode_nick FROM users")
    rows = cur.fetchall()
    conn.close()
    return rows


def save_daily_stats(day: str, tid: int, solved_count: int, titles: List[str]):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "REPLACE INTO daily_stats(day, telegram_id, solved_count, titles_json) VALUES(?,?,?,?)",
        (day, tid, int(solved_count), json.dumps(titles, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def get_week_stats(days: List[str]):
    """
    Returns dict: tid -> {day -> solved_count}
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in days)
    cur.execute(
        f"SELECT day, telegram_id, solved_count FROM daily_stats WHERE day IN ({placeholders})",
        tuple(days),
    )
    rows = cur.fetchall()
    conn.close()

    out: Dict[int, Dict[str, int]] = {}
    for d, tid, cnt in rows:
        out.setdefault(int(tid), {})[d] = int(cnt)
    return out


# ----------------- LeetCode helpers -----------------
def nick_escape(s: str) -> str:
    return s.replace('"', '\\"')


def leetcode_recent_submissions(nick: str):
    q = (
        '{ recentSubmissionList(username: "%s") { title timestamp statusDisplay } }'
        % nick_escape(nick)
    )
    resp = requests.post(LEETCODE_GRAPHQL, json={"query": q}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", {}).get("recentSubmissionList") or []


def accepted_titles_on_day(nick: str, target_day: date) -> Tuple[Optional[List[str]], Optional[str]]:
    """
    Returns (titles, err).
    titles is unique list of problem titles with Accepted submissions on target_day.
    """
    day_key = target_day.strftime("%Y-%m-%d")
    cache_key = (nick, day_key)

    now_ts = datetime.now(TZ).timestamp()
    cached = _cache.get(cache_key)
    if cached and (now_ts - cached[1] < CACHE_TTL_SECONDS):
        return cached[0], None

    try:
        subs = leetcode_recent_submissions(nick)
    except Exception as e:
        logger.exception("LeetCode fetch error for %s: %s", nick, e)
        return None, str(e)

    titles: List[str] = []
    seen = set()

    for item in subs:
        ts = item.get("timestamp")
        if ts is None:
            continue
        try:
            ts_int = int(ts)
        except Exception:
            continue

        # seconds vs ms
        if ts_int > 1_000_000_000_000:
            ts_int //= 1000

        dt = datetime.fromtimestamp(ts_int, tz=TZ)
        if dt.date() != target_day:
            continue

        if item.get("statusDisplay") != "Accepted":
            continue

        title = item.get("title") or "Unknown"
        if title not in seen:
            seen.add(title)
            titles.append(title)

    _cache[cache_key] = (titles, now_ts)
    return titles, None


def accepted_titles_today(nick: str) -> Tuple[Optional[List[str]], Optional[str]]:
    return accepted_titles_on_day(nick, datetime.now(TZ).date())


def mention(uname: str) -> str:
    # if stored username already contains @, keep it; otherwise try to @mention
    if uname and uname.startswith("@"):
        return uname
    if uname and " " not in uname:
        return f"@{uname}"
    return uname or "unknown"


# ----------------- Telegram handlers -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Yo! Я LeetCode-бот.\n\n"
        "Команды:\n"
        "• /register <nick>\n"
        "• /unregister\n"
        "• /check — твои задачи сегодня\n"
        "• /list — статус всех сегодня\n"
        "• /list @user — задачи пользователя сегодня\n"
        "• /week — статистика за 7 дней\n"
        "• /setgroup — (админ) куда слать напоминания\n\n"
        "Правило простое: минимум 1 задача в день ✅"
    )


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) != 1:
        await update.message.reply_text("Использование: /register <leetcode_nick>")
        return
    nick = context.args[0].strip()
    add_user(user.id, user.username or user.full_name, nick)
    await update.message.reply_text(f"🔥 Готово! Ты зарегистрирован как: {nick}")


async def unregister(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remove_user(update.effective_user.id)
    await update.message.reply_text("🫡 Удалил. Но я верю, ты вернёшься сильнее.")


async def setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Команда /setgroup должна быть вызвана в группе.")
        return

    member = await chat.get_member(user.id)
    if member.status not in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
        await update.message.reply_text("Только админ может назначить группу 😄")
        return

    db_set_config("report_chat_id", str(chat.id))
    await update.message.reply_text("📌 Ок! Эта группа теперь получает напоминания/отчёты.")


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /check — персонально: сколько задач ты решил сегодня и какие.
    """
    user = update.effective_user
    rows = list_users()
    nick = None
    uname = user.username or user.full_name

    for tid, _, lnick in rows:
        if int(tid) == int(user.id):
            nick = lnick
            break

    if not nick:
        await update.message.reply_text("Ты не зарегистрирован. Используй /register <nick> 👀")
        return

    titles, err = accepted_titles_today(nick)
    today = datetime.now(TZ).strftime("%Y-%m-%d")

    if err:
        await update.message.reply_text(f"⚠️ Ошибка при проверке LeetCode: {err}")
        return

    if not titles:
        await update.message.reply_text(f"😴 {mention(uname)}, сегодня ({today}) пока 0 задач. Пора спасать статистику!")
        return

    msg = (
        f"🔥 {mention(uname)}, сегодня ({today}) ты решил {len(titles)} задач:\n"
        + "\n".join([f"• {t}" for t in titles])
    )
    await update.message.reply_text(msg)


async def listcmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /list — ДЛЯ ВСЕХ: статус всех сегодня (кол-во задач + ✅/❌)
    /list @user — ДЛЯ ВСЕХ: задачи пользователя сегодня
    """
    rows = list_users()
    if not rows:
        await update.message.reply_text("Список пуст. Кто первый: /register <nick> 😄")
        return

    # /list @user OR /list <leetcodeNick>
    if len(context.args) == 1:
        target = context.args[0].strip()
        nick = None
        display = target

        if target.startswith("@"):
            t = target.lower()
            for _, uname, lnick in rows:
                if (mention(uname)).lower() == t:
                    nick = lnick
                    display = mention(uname)
                    break
        else:
            # allow direct leetcode nick
            nick = target

        if not nick:
            await update.message.reply_text("Не нашёл пользователя. Используй /list @username или /list <leetcode_nick>.")
            return

        titles, err = accepted_titles_today(nick)
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        if err:
            await update.message.reply_text(f"⚠️ Ошибка при проверке {display}: {err}")
            return

        if not titles:
            await update.message.reply_text(f"{display} — сегодня ({today}) 0 задач ❌")
            return

        msg = f"{display} — сегодня ({today}) решил {len(titles)} задач ✅:\n" + "\n".join([f"• {t}" for t in titles])
        await update.message.reply_text(msg)
        return

    # /list - summary
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    header = f"📋 Сегодняшний статус — {today_str}\n(цель: ≥1 задача)\n"
    lines = []
    for _, uname, nick in rows:
        titles, err = accepted_titles_today(nick)
        if err:
            lines.append(f"{mention(uname)} — ❓ ошибка проверки")
            continue
        cnt = len(titles or [])
        mark = "✅" if cnt >= 1 else "❌"
        lines.append(f"{mention(uname)} — {cnt} задач {mark}")

    await update.message.reply_text(header + "\n" + "\n".join(lines))


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /week — 7-day totals for everyone (from stored daily snapshots)
    /week @user — 7-day breakdown for one user
    """
    rows = list_users()
    if not rows:
        await update.message.reply_text("Пока нет зарегистрированных. /register <nick>")
        return

    today = datetime.now(TZ).date()
    days = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]  # oldest..today
    week_map = get_week_stats(days)

    # /week @user
    if len(context.args) == 1:
        target = context.args[0].strip()
        target_tid = None
        target_uname = target

        if target.startswith("@"):
            t = target.lower()
            for tid, uname, _ in rows:
                if mention(uname).lower() == t:
                    target_tid = int(tid)
                    target_uname = mention(uname)
                    break
        else:
            # allow by LeetCode nick
            for tid, uname, nick in rows:
                if nick.lower() == target.lower():
                    target_tid = int(tid)
                    target_uname = mention(uname)
                    break

        if target_tid is None:
            await update.message.reply_text("Не нашёл пользователя. Используй /week @username или /week <leetcode_nick>.")
            return

        per_day = week_map.get(target_tid, {})
        total = sum(per_day.get(d, 0) for d in days)
        msg = [f"📅 Неделя для {target_uname} (последние 7 дней):", f"Итого: {total} задач\n"]
        for d in days:
            msg.append(f"{d}: {per_day.get(d, 0)}")
        await update.message.reply_text("\n".join(msg))
        return

    # /week summary
    msg_lines = ["📊 Статистика за неделю (последние 7 дней):", "(данные из ежедневных снимков бота)\n"]
    scores = []
    for tid, uname, _ in rows:
        per_day = week_map.get(int(tid), {})
        total = sum(per_day.get(d, 0) for d in days)
        scores.append((total, mention(uname)))

    scores.sort(reverse=True, key=lambda x: x[0])
    for total, uname in scores:
        trophy = "🏆" if total == scores[0][0] and total > 0 else ""
        msg_lines.append(f"{uname}: {total} задач {trophy}")

    await update.message.reply_text("\n".join(msg_lines))


# ----------------- Jobs: reminder + daily report -----------------
async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Every 3 hours:
    - ping those who have 0 accepted today (with mentions)
    - if everyone has >=1, send celebration once per day
    """
    chat_id = db_get_config("report_chat_id")
    if not chat_id:
        return
    chat_id = int(chat_id)

    rows = list_users()
    if not rows:
        return

    today = datetime.now(TZ).date()
    today_str = today.strftime("%Y-%m-%d")

    not_done = []
    for _, uname, nick in rows:
        titles, err = accepted_titles_on_day(nick, today)
        if err:
            continue
        if not titles:
            not_done.append(mention(uname))

    # Everyone done -> celebrate once/day
    if not not_done:
        flag_key = f"all_done_{today_str}"
        if not db_get_config(flag_key):
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🎉 ВСЕ МОЛОДЦЫ!\n\n"
                    "Каждый решил минимум 1 задачу сегодня 💪🔥\n"
                    "Группа официально НЕ ленивая 😎"
                ),
            )
            db_set_config(flag_key, "1")
        return

    # Still slackers -> fun ping
    emojis = ["⏰", "🚨", "👀", "🧠", "🔥"]
    e = emojis[int(datetime.now(TZ).timestamp()) % len(emojis)]
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"{e} Напоминалка: сегодня ещё без задач:\n" + ", ".join(not_done) + "\n\n"
            "Правило простое: минимум 1 задача. Погнали! 🚀"
    )


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    """
    End-of-day report:
    - saves today's counts to daily_stats
    - posts status list + MVP day
    """
    chat_id = db_get_config("report_chat_id")
    if not chat_id:
        return
    chat_id = int(chat_id)

    rows = list_users()
    if not rows:
        await context.bot.send_message(chat_id=chat_id, text="Сегодня никого не было в списке 😄")
        return

    today = datetime.now(TZ).date()
    today_str = today.strftime("%Y-%m-%d")

    report_lines = []
    mvp = ("", -1)  # (uname, count)

    for tid, uname, nick in rows:
        titles, err = accepted_titles_on_day(nick, today)
        if err:
            report_lines.append(f"{mention(uname)} — ❓ ошибка проверки")
            save_daily_stats(today_str, int(tid), 0, [])
            continue

        titles = titles or []
        cnt = len(titles)
        mark = "✅" if cnt >= 1 else "❌"
        report_lines.append(f"{mention(uname)} — {cnt} задач {mark}")

        save_daily_stats(today_str, int(tid), cnt, titles)

        if cnt > mvp[1]:
            mvp = (mention(uname), cnt)

    # MVP message
    if mvp[1] <= 0:
        mvp_line = "🏆 MVP дня: сегодня без победителей… но завтра новый шанс 😄"
    else:
        mvp_line = f"🏆 MVP дня: {mvp[0]} — {mvp[1]} задач(и) 🔥"

    header = f"🧾 Итог дня — {today_str}\n(цель: ≥1 задача)\n"
    text = header + "\n".join(report_lines) + "\n\n" + mvp_line
    await context.bot.send_message(chat_id=chat_id, text=text)

async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "ℹ️ *Информация о боте*\n\n Я слежу за тем, чтобы каждый решал *минимум 1 задачу в день* на LeetCode 💪\n\n *Как начать:*\n 1. Добавь бота в группу\n 2️⃣ Сделай бота администратором\n 3️⃣ В группе напиши /setgroup\n 4️⃣ Каждый участник пишет /register <leetcode_nick>\n\n *Команды:*\n • /register <nick> — зарегистрировать LeetCode ник\n • /unregister — удалить себя из бота\n • /check — сколько и какие задачи *ты* решил сегодня\n • /list — статус всех за сегодня (кол-во + ✅/❌)\n • /list @user — какие задачи решил пользователь сегодня\n • /week — статистика за последние 7 дней\n • /week @user — статистика за 7 дней для конкретного пользователя\n • /info — эта справка\n\n *Авто-логика:*\n ⏰ Каждые 3 часа бот пингует тех, кто ещё не решил ни одной задачи\n 🎉 Как только *все* решат ≥1 задачу — бот поздравит группу\n 🏆 В конце дня бот отправляет отчёт + MVP дня\n\n Правило простое: *1 задача в день — и ты красавчик* 😎"
    try:
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(text)  # без форматирования

    # await update.message.reply_text(
    #     parse_mode="Markdown"
    # )


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
    app.add_handler(CommandHandler("setgroup", setgroup))
    app.add_handler(CommandHandler("list", listcmd))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("week", week))
    app.add_handler(CommandHandler("info", info))

    # Every 3 hours reminder
    app.job_queue.run_repeating(reminder_job, interval=REMINDER_INTERVAL_SECONDS, first=10)

    # End-of-day report (and stats snapshot)
    app.job_queue.run_daily(
        daily_report_job,
        time=time(hour=DAILY_HOUR, minute=DAILY_MINUTE, tzinfo=TZ),
    )

    print("Bot started. Press Ctrl-C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
