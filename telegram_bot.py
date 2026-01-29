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
- /backup                     — (админ) прислать файл базы (регистрации/статистика) для переноса

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
DB_PATH = os.getenv("DB_PATH", "leetcode_bot.db")
DB_SCHEMA_VERSION = 1
LEETCODE_GRAPHQL = "https://leetcode.com/graphql"
TZ = ZoneInfo("Asia/Almaty")

DAILY_HOUR = int(os.getenv("DAILY_HOUR", "22"))
DAILY_MINUTE = int(os.getenv("DAILY_MINUTE", "00"))

REMINDER_INTERVAL_SECONDS = 3 * 60 * 60  # 3 hours
CACHE_TTL_SECONDS = 120  # 2 minutes, to avoid repeated API calls spam

ADMIN_IDS = {int(x) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().isdigit()}  # optional
OWNER_ID = int(os.getenv('OWNER_ID', '0'))  # your personal Telegram user_id; set in Railway Variables

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

    ensure_db_schema()



def ensure_db_schema():
    """
    Simple schema-versioning for SQLite.
    Stores current version in config key 'db_schema_version'.
    This allows safe future changes (add tables/columns) without losing user data.
    """
    target = DB_SCHEMA_VERSION
    current_raw = db_get_config("db_schema_version")
    try:
        current = int(current_raw) if current_raw is not None else 0
    except Exception:
        current = 0

    if current >= target:
        return

    # --- Migrations ---
    # v1: initial schema (users/config/daily_stats)
    # Tables are created with CREATE TABLE IF NOT EXISTS, so we only need to record the version.
    if current < 1:
        db_set_config("db_schema_version", "1")
        current = 1

    # If you add new migrations later, extend like:
    # if current < 2: ...; db_set_config("db_schema_version","2"); current=2

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
        '{ recentSubmissionList(username: "%s") { title titleSlug timestamp statusDisplay } }'
        % nick_escape(nick)
    )
    resp = requests.post(LEETCODE_GRAPHQL, json={"query": q}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", {}).get("recentSubmissionList") or []



# difficulty cache: titleSlug -> (DIFFICULTY, fetched_at_epoch)
_diff_cache: Dict[str, Tuple[str, float]] = {}
_DIFF_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h


def leetcode_question_difficulty(title_slug: Optional[str]) -> str:
    """Return difficulty for a LeetCode problem by titleSlug (EASY/MEDIUM/HARD)."""
    if not title_slug:
        return "UNKNOWN"

    now_ts = datetime.now(TZ).timestamp()
    cached = _diff_cache.get(title_slug)
    if cached and (now_ts - cached[1] < _DIFF_CACHE_TTL_SECONDS):
        return cached[0]

    q = '{ question(titleSlug: "%s") { difficulty } }' % nick_escape(title_slug)
    try:
        resp = requests.post(LEETCODE_GRAPHQL, json={"query": q}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        diff = (data.get("data", {}).get("question", {}) or {}).get("difficulty") or "UNKNOWN"
    except Exception as e:
        logger.exception("LeetCode difficulty fetch error for %s: %s", title_slug, e)
        diff = "UNKNOWN"

    diff_up = str(diff).upper()
    if diff_up not in ("EASY", "MEDIUM", "HARD"):
        diff_up = "UNKNOWN"
    _diff_cache[title_slug] = (diff_up, now_ts)
    return diff_up


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
        slug = item.get("titleSlug") or None
        key = slug or title
        if key not in seen:
            seen.add(key)
            diff = leetcode_question_difficulty(slug)
            titles.append(f"{diff} {title}")

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


def _now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


async def maybe_set_group_chat(update: Update):
    """
    Надёжность для Railway Trial:
    - Если бот получает любую команду в группе/супергруппе, запоминаем chat_id как report_chat_id.
    Это нужно, потому что на Railway Trial БД может очищаться после деплоя и /setgroup забывается.
    """
    try:
        chat = update.effective_chat
        if chat and chat.type in ("group", "supergroup"):
            cur = db_get_config("report_chat_id")
            if cur != str(chat.id):
                db_set_config("report_chat_id", str(chat.id))
                logger.info("Auto-set report_chat_id=%s at %s", chat.id, _now_str())
    except Exception as e:
        logger.warning("maybe_set_group_chat failed: %s", e)


def _get_last_report_day() -> Optional[str]:
    return db_get_config("last_daily_report_day")


def _set_last_report_day(day_str: str):
    db_set_config("last_daily_report_day", day_str)

async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Admin check:
    - If env ADMIN_IDS is set (comma-separated user IDs), only those users can use admin commands anywhere.
    - Otherwise: in groups, chat admins/owners can use; in private chats -> deny (ask to set ADMIN_IDS).
    """
    user = update.effective_user
    chat = update.effective_chat

    if ADMIN_IDS:
        return user and user.id in ADMIN_IDS

    if chat and chat.type in ("group", "supergroup"):
        try:
            member = await chat.get_member(user.id)
            return member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
        except Exception:
            return False

    # private / unknown chat: require ADMIN_IDS for safety
    return False


async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    """
    /backup — admin-only: sends the SQLite DB file to the admin as a document.
    Useful before moving to VPS (Oracle) or after redeploys on ephemeral storage.
    """
    if not await _is_admin(update, context):
        await update.message.reply_text(
            "⛔ Эта команда только для админа.\n"
            "Если хочешь включить админ-доступ в личке — задай переменную ADMIN_IDS (id через запятую)."
        )
        return

    db_path = DB_PATH
    if not os.path.exists(db_path):
        await update.message.reply_text(f"База не найдена по пути: {db_path}")
        return

    # A tiny “fun” message
    await update.message.reply_text("🧳 Пакую базу в чемодан… ща прилетит 📦")

    try:
        with open(db_path, "rb") as f:
            filename = os.path.basename(db_path)
            await update.message.reply_document(document=f, filename=filename, caption="✅ Вот бэкап базы. Не теряй 😄")
    except Exception as e:
        logger.exception("Backup send failed: %s", e)
        await update.message.reply_text(f"⚠️ Не смог отправить файл базы: {e}")



async def restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /restore — hidden owner-only:
    Send JSON backup file and reply to it with /restore.
    Not advertised in /start or /info.
    """
    user = update.effective_user
    if not user or not OWNER_ID or user.id != OWNER_ID:
        await update.message.reply_text("⛔ Нет доступа.")
        return

    msg = update.message
    doc = None
    if msg and msg.document:
        doc = msg.document
    elif msg and msg.reply_to_message and msg.reply_to_message.document:
        doc = msg.reply_to_message.document

    if not doc:
        await update.message.reply_text(
            "Пришли JSON-бэкап файлом и ответь на него командой /restore."
            "Пример: отправляешь backup.json → reply → /restore"
        )
        return

    if not (doc.file_name or "").lower().endswith(".json"):
        await update.message.reply_text("Нужен .json файл бэкапа.")
        return

    await update.message.reply_text("🧩 Ок, распаковываю бэкап… (не дыши)")

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        raw = await tg_file.download_as_bytearray()
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        logger.exception("Restore read/parse failed: %s", e)
        await update.message.reply_text(f"⚠️ Не смог прочитать/распарсить JSON: {e}")
        return

    schema_version = data.get("schema_version") or data.get("db_schema_version") or 1
    try:
        schema_version = int(schema_version)
    except Exception:
        schema_version = 1

    users_rows = data.get("users")
    stats_rows = data.get("daily_stats")
    config_rows = data.get("config")

    if isinstance(data.get("tables"), dict):
        t = data["tables"]
        users_rows = users_rows or t.get("users")
        stats_rows = stats_rows or t.get("daily_stats")
        config_rows = config_rows or t.get("config")

    if users_rows is None and stats_rows is None and config_rows is None:
        await update.message.reply_text("⚠️ Это не похоже на бэкап (нет users/daily_stats/config).")
        return

    try:
        # Make sure schema exists
        init_db()

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        # Clear existing data
        cur.execute("DELETE FROM users")
        cur.execute("DELETE FROM daily_stats")
        cur.execute("DELETE FROM config")

        # Restore config
        if config_rows:
            if isinstance(config_rows, dict):
                for k, v in config_rows.items():
                    cur.execute("REPLACE INTO config(key,value) VALUES(?,?)", (str(k), str(v)))
            else:
                for row in config_rows:
                    k = row.get("key")
                    v = row.get("value")
                    if k is not None and v is not None:
                        cur.execute("REPLACE INTO config(key,value) VALUES(?,?)", (str(k), str(v)))

        # Record schema version
        cur.execute("REPLACE INTO config(key,value) VALUES(?,?)", ("db_schema_version", str(schema_version)))

        # Restore users
        users_count = 0
        if users_rows:
            for row in users_rows:
                tid = row.get("telegram_id") or row.get("tid") or row.get("id")
                uname = row.get("username") or row.get("uname") or ""
                nick = row.get("leetcode_nick") or row.get("nick") or row.get("leetcode") or ""
                if tid is None or not nick:
                    continue
                cur.execute(
                    "REPLACE INTO users(telegram_id, username, leetcode_nick) VALUES(?,?,?)",
                    (int(tid), str(uname), str(nick)),
                )
                users_count += 1

        # Restore daily_stats
        stats_count = 0
        if stats_rows:
            for row in stats_rows:
                day = row.get("day")
                tid = row.get("telegram_id") or row.get("tid")
                cnt = row.get("solved_count") or row.get("count") or 0
                titles_json = row.get("titles_json")
                titles = row.get("titles")
                if titles_json is None and titles is not None:
                    titles_json = json.dumps(titles, ensure_ascii=False)
                if day is None or tid is None:
                    continue
                cur.execute(
                    "REPLACE INTO daily_stats(day, telegram_id, solved_count, titles_json) VALUES(?,?,?,?)",
                    (str(day), int(tid), int(cnt), titles_json or "[]"),
                )
                stats_count += 1

        conn.commit()
        conn.close()

        _cache.clear()

        await update.message.reply_text(
            "✅ Восстановление завершено!"
            f"• users: {users_count}"
            f"• daily_stats: {stats_count}"
            "Можно продолжать: /list, /week."
        )
    except Exception as e:
        logger.exception("Restore failed: %s", e)
        await update.message.reply_text(f"❌ Restore упал: {e}")


# ----------------- Telegram handlers -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    await update.message.reply_text(
        "🤖 Yo! Я LeetCode-бот.\n\n"
        "Команды:\n"
        "• /register <nick>\n"
        "• /unregister\n"
        "• /check — твои задачи сегодня\n"
        "• /list — статус всех сегодня\n"
        "• /list @user — задачи пользователя сегодня\n"
        "• /week — статистика за 7 дней\n"
        "• /setgroup — (админ) куда слать напоминания\n"
        "• /info — полная информация по боту и командам\n\n"
        "Правило простое: минимум 1 задача в день ✅"
    )


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    user = update.effective_user
    if len(context.args) != 1:
        await update.message.reply_text("Использование: /register <leetcode_nick>")
        return
    nick = context.args[0].strip()
    add_user(user.id, user.username or user.full_name, nick)

    # сброс кеша на сегодня для нового ника, чтобы /list сразу показал актуально
    today_key = datetime.now(TZ).strftime("%Y-%m-%d")
    _cache.pop((nick, today_key), None)

    # небольшая проверка: сколько уже решено сегодня
    titles, err = accepted_titles_today(nick)
    if err:
        await update.message.reply_text(
            f"🔥 Готово! Ты зарегистрирован как: {nick}"
            "⚠️ Но LeetCode сейчас не ответил — позже /check покажет всё нормально."
        )
        return

    cnt = len(titles or [])
    mark = "✅" if cnt >= 1 else "❌"
    await update.message.reply_text(
        f"🔥 Готово! Ты зарегистрирован как: {nick}"
        f"Сегодня у тебя: {cnt} задач {mark}"
        "Теперь ты в общем списке /list (если вдруг не видно — сделай /list ещё раз через 5–10 сек, Telegram иногда доставляет апдейты пачкой 😄)"
    )


async def unregister(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    remove_user(update.effective_user.id)
    await update.message.reply_text("🫡 Удалил. Но я верю, ты вернёшься сильнее.")


async def setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
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
    await maybe_set_group_chat(update)
    """
    /check — если без аргументов: твои задачи сегодня.
    /check @user — задачи указанного пользователя сегодня.
    /check <leetcode_nick> — задачи по нику LeetCode.
    """
    rows = list_users()
    if not rows:
        await update.message.reply_text("Список пуст. Кто первый: /register <nick> 😄")
        return

    user = update.effective_user

    target_display = mention(user.username or user.full_name)
    nick = None

    # With argument: /check @user or /check <leetcodeNick>
    if len(context.args) == 1:
        target = context.args[0].strip()
        if target.startswith("@"):
            t = target.lower()
            for _, uname, lnick in rows:
                if mention(uname).lower() == t:
                    nick = lnick
                    target_display = mention(uname)
                    break
        else:
            # allow direct leetcode nick
            nick = target
            target_display = target
    else:
        # no args -> self
        for tid, _, lnick in rows:
            if int(tid) == int(user.id):
                nick = lnick
                break

    if not nick:
        if len(context.args) == 1 and context.args[0].strip().startswith("@"):
            await update.message.reply_text("Не нашёл пользователя в базе. Пусть он сделает /register <nick> 👀")
        else:
            await update.message.reply_text("Ты не зарегистрирован. Используй /register <nick> 👀")
        return

    titles, err = accepted_titles_today(nick)
    today = datetime.now(TZ).strftime("%Y-%m-%d")

    if err:
        await update.message.reply_text(f"⚠️ Ошибка при проверке LeetCode: {err}")
        return

    titles = titles or []
    if not titles:
        await update.message.reply_text(f"😴 {target_display}, сегодня ({today}) пока 0 задач. Пора спасать статистику!")
        return

    msg = (
        f"🔥 {target_display}, сегодня ({today}) решено {len(titles)} задач:\n"
        + "\n".join([f"• {t}" for t in titles])
    )
    await update.message.reply_text(msg)


async def listcmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
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

    scored = []  # (cnt, name, line, had_error)
    for _, uname, nick in rows:
        titles, err = accepted_titles_today(nick)
        if err:
            name = mention(uname)
            scored.append((-1, name, f"{name} — ❓ ошибка проверки", True))
            continue

        cnt = len(titles or [])
        mark = "✅" if cnt >= 1 else "❌"
        name = mention(uname)
        scored.append((cnt, name, f"{name} — {cnt} задач {mark}", False))

    # Sort: more solved -> top; zeros -> bottom; errors -> very bottom
    scored.sort(key=lambda x: (x[3], -x[0], x[1].lower()))

    lines = [line for _, __, line, ___ in scored]
    await update.message.reply_text(header + "\n" + "\n".join(lines))


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
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
    logger.info("Reminder tick at %s", _now_str())
    """
    Every 3 hours:
    - ping those who have 0 accepted today (with mentions)
    - if everyone has >=1, send celebration once per day
    """
    chat_id_raw = db_get_config("report_chat_id")
    if not chat_id_raw:
        logger.info("Reminder skipped: report_chat_id not set")
        return
    chat_id = int(chat_id_raw)

    rows = list_users()
    if not rows:
        logger.info("Reminder skipped: no users registered")
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
                    "🎉 ВСЕ МОЛОДЦЫ!"
                    "Каждый решил минимум 1 задачу сегодня 💪🔥"
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
        text=(
            f"{e} Напоминалка: сегодня ещё без задач:"
            + ", ".join(not_done)
            + "Правило простое: минимум 1 задача. Погнали! 🚀"
        ),
    )


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE, target_day: Optional[date] = None):
    """
    End-of-day report:
    - saves today's counts to daily_stats
    - posts status list + MVP day
    - target by the value of target_day (for catch-up job); otherwise uses today
    """
    chat_id = db_get_config("report_chat_id")
    if not chat_id:
        return
    chat_id = int(chat_id)

    rows = list_users()
    if not rows:
        await context.bot.send_message(chat_id=chat_id, text="Сегодня никого не было в списке 😄")
        return

    day = target_day or datetime.now(TZ).date()
    day_str = day.strftime("%Y-%m-%d")

    report_lines = []
    mvp = ("", -1)  # (uname, count)

    for tid, uname, nick in rows:
        titles, err = accepted_titles_on_day(nick, day)
        if err:
            report_lines.append(f"{mention(uname)} — ❓ ошибка проверки")
            save_daily_stats(day_str, int(tid), 0, [])
            continue

        titles = titles or []
        cnt = len(titles)
        mark = "✅" if cnt >= 1 else "❌"
        report_lines.append(f"{mention(uname)} — {cnt} задач {mark}")

        save_daily_stats(day_str, int(tid), cnt, titles)

        if cnt > mvp[1]:
            mvp = (mention(uname), cnt)

    # MVP message
    if mvp[1] <= 0:
        mvp_line = "🏆 MVP дня: сегодня без победителей… но завтра новый шанс 😄"
    else:
        mvp_line = f"🏆 MVP дня: {mvp[0]} — {mvp[1]} задач(и) 🔥"

    header = f"🧾 Итог дня — {day_str}\n(цель: ≥1 задача)\n"
    text = header + "\n".join(report_lines) + "\n\n" + mvp_line
    await context.bot.send_message(chat_id=chat_id, text=text)

async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    text = "ℹ️ *Информация о боте*\n\n Я слежу за тем, чтобы каждый решал *минимум 1 задачу в день* на LeetCode 💪\n\n *Как начать:*\n 1️⃣Каждый участник пишет /register <leetcode_nick>\n\n *Команды:*\n • /register <nick> — зарегистрировать LeetCode ник\n • /unregister — удалить себя из бота\n • /check — сколько и какие задачи *ты* решил сегодня\n • /list — статус всех за сегодня (кол-во + ✅/❌)\n • /list @user — какие задачи решил пользователь сегодня\n • /week — статистика за последние 7 дней\n • /week @user — статистика за 7 дней для конкретного пользователя\n • /info — эта справка\n\n *Авто-логика:*\n ⏰ Каждые 3 часа бот пингует тех, кто ещё не решил ни одной задачи\n 🎉 Как только *все* решат ≥1 задачу — бот поздравит группу\n 🏆 В конце дня бот отправляет отчёт + MVP дня\n\n Правило простое: *1 задача в день — и ты красавчик* 😎"
    try:
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(text);



async def catchup_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Run once after startup:
    if yesterday's daily report was missed, generate it now.
    """
    try:
        last_day = _get_last_report_day()
        y = datetime.now(TZ).date() - timedelta(days=1)
        y_str = y.strftime("%Y-%m-%d")
        if last_day == y_str:
            return
        if not db_get_config("report_chat_id"):
            return
        logger.info("Catch-up job: generating report for %s at %s", y_str, _now_str())
        await daily_report_job(context, target_day=y)
    except Exception as e:
        logger.exception("catchup_job failed: %s", e)


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
    app.add_handler(CommandHandler("backup", backup))
    app.add_handler(CommandHandler("restore", restore))  # hidden owner-only

    app.job_queue.run_repeating(reminder_job, interval=REMINDER_INTERVAL_SECONDS, first=10)

    # End-of-day report (and stats snapshot)
    app.job_queue.run_daily(
        daily_report_job,
        time=time(hour=DAILY_HOUR, minute=DAILY_MINUTE, tzinfo=TZ),
    )

    # Catch-up once shortly after start (если бот был оффлайн в момент отчёта)
    app.job_queue.run_once(catchup_job, when=30)

    print("Bot started. Press Ctrl-C to stop.")
    app.run_polling()



if __name__ == "__main__":
    main()