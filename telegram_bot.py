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


добавь функцию remove

+ идея улучшить лидерборд через важность уровня задачи 
Мысалга
Изи - 1 балл
Медиум - 3 балл
Хард - 5 балл
Сонда лидерборд будет честным учитывая кол-во задач + важность

починить mvp дня, чтобы не показывался не последний участник в случае ничьи а оба.

админ команды.

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
DB_SCHEMA_VERSION = 3
LEETCODE_GRAPHQL = "https://leetcode.com/graphql"
TZ = ZoneInfo("Asia/Almaty")
LEETCODE_RECENT_ACCEPTED_LIMIT = int(os.getenv("LEETCODE_RECENT_ACCEPTED_LIMIT", "100"))
TASK_SLUG_SEP = "||"

DAILY_HOUR = int(os.getenv("DAILY_HOUR", "23"))
DAILY_MINUTE = int(os.getenv("DAILY_MINUTE", "58"))

REMINDER_INTERVAL_SECONDS = 5 * 60 * 60  # 5 hours
CACHE_TTL_SECONDS = 120  # 2 minutes, to avoid repeated API calls spam

ADMIN_IDS = {int(x) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().isdigit()}  # optional
OWNER_ID = int(os.getenv('OWNER_ID', '0'))  # your personal Telegram user_id; set in Railway Variables

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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

    # leaderboard: persistent points (all-time)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS leaderboard (
            telegram_id INTEGER PRIMARY KEY,
            points INTEGER
        )
        """
    )

    # warns: disciplinary system (telegram_id -> warn count)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS warns (
            telegram_id INTEGER PRIMARY KEY,
            count INTEGER
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
    if current < 1:
        db_set_config("db_schema_version", "1")
        current = 1

    # v2: leaderboard table (telegram_id -> points)
    if current < 2:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS leaderboard (
                telegram_id INTEGER PRIMARY KEY,
                points INTEGER
            )
            """
        )
        conn.commit()
        conn.close()
        db_set_config("db_schema_version", "2")
        current = 2

    # v3: warns table (telegram_id -> warn count)
    if current < 3:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS warns (
                telegram_id INTEGER PRIMARY KEY,
                count INTEGER
            )
            """
        )
        conn.commit()
        conn.close()
        db_set_config("db_schema_version", "3")
        current = 3


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

def _points_for_difficulty(diff: str) -> int:
    d = (diff or "").strip().upper()
    if d == "EASY":
        return 1
    if d == "MEDIUM":
        return 3
    if d == "HARD":
        return 5
    return 0


def points_from_titles(titles: List[str]) -> int:
    total = 0
    for t in titles or []:
        diff, _title, _slug = _parse_task_entry(t)
        total += _points_for_difficulty(diff)
    return total


def _get_leaderboard_reset_day() -> str:
    return db_get_config("leaderboard_reset_day") or ""


def recompute_leaderboard_from_daily_stats():
    reset_day = _get_leaderboard_reset_day()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM leaderboard")

    if reset_day:
        cur.execute("SELECT telegram_id, titles_json FROM daily_stats WHERE day >= ?", (reset_day,))
    else:
        cur.execute("SELECT telegram_id, titles_json FROM daily_stats")
    rows = cur.fetchall()

    totals: Dict[int, int] = {}
    for tid, titles_json in rows:
        try:
            titles = json.loads(titles_json or "[]")
        except Exception:
            titles = []
        totals[int(tid)] = totals.get(int(tid), 0) + points_from_titles(titles)

    for tid, pts in totals.items():
        cur.execute("REPLACE INTO leaderboard(telegram_id, points) VALUES(?,?)", (int(tid), int(pts)))

    conn.commit()
    conn.close()


def get_leaderboard_points() -> Dict[int, int]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT telegram_id, points FROM leaderboard")
    rows = cur.fetchall()
    conn.close()
    return {int(tid): int(pts or 0) for tid, pts in rows}


# ----------------- Warn system helpers -----------------
def get_warn_count(tid: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT count FROM warns WHERE telegram_id=?", (int(tid),))
    row = cur.fetchone()
    conn.close()
    return int(row[0] or 0) if row else 0


def set_warn_count(tid: int, count: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("REPLACE INTO warns(telegram_id, count) VALUES(?,?)", (int(tid), int(count)))
    conn.commit()
    conn.close()


def inc_warn(tid: int) -> int:
    cur_count = get_warn_count(tid)
    new_count = cur_count + 1
    set_warn_count(tid, new_count)
    return new_count


def clear_warns(tid: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM warns WHERE telegram_id=?", (int(tid),))
    conn.commit()
    conn.close()


def clear_all_warns():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM warns")
    conn.commit()
    conn.close()

def _parse_task_entry(s: str) -> Tuple[str, str, str]:
    """Parse stored task entry into (difficulty, title, titleSlug). Backward-compatible with old rows."""
    try:
        raw = s or ""
        if TASK_SLUG_SEP in raw:
            raw, slug = raw.rsplit(TASK_SLUG_SEP, 1)
            slug = (slug or "").strip()
        else:
            slug = ""

        parts = raw.split(" ", 1)
        if len(parts) == 2:
            diff = (parts[0] or "").strip().upper() or "UNKNOWN"
            title = (parts[1] or "").strip() or "Unknown"
            if diff not in ("EASY", "MEDIUM", "HARD"):
                diff = "UNKNOWN"
            return diff, title, slug
    except Exception:
        pass
    return "UNKNOWN", (s or "").strip() or "Unknown", ""


def _encode_task_entry(diff: str, title: str, slug: Optional[str]) -> str:
    diff_up = (diff or "UNKNOWN").strip().upper()
    if diff_up not in ("EASY", "MEDIUM", "HARD"):
        diff_up = "UNKNOWN"
    clean_title = (title or "Unknown").strip() or "Unknown"
    clean_slug = (slug or "").strip()
    if clean_slug:
        return f"{diff_up} {clean_title}{TASK_SLUG_SEP}{clean_slug}"
    return f"{diff_up} {clean_title}"


def _format_task_entry(s: str) -> str:
    diff, title, slug = _parse_task_entry(s)
    points = _points_for_difficulty(diff)
    prefix = f"{diff} (+{points})" if points else diff
    if slug:
        return f"{prefix} {title} — https://leetcode.com/problems/{slug}/"
    return f"{prefix} {title}"


def _merge_titles(old_titles: List[str], new_titles: List[str]) -> List[str]:
    """
    Merge today's titles in a monotonic way:
    - Never drops previously seen tasks (prevents negative deltas if LC recent list is limited).
    - If difficulty was UNKNOWN and later becomes known, we upgrade it (prevents undercount).
    Key is the problem title (without difficulty prefix).
    """
    by_key: Dict[str, Tuple[str, str, str]] = {}

    for t in old_titles or []:
        diff, title, slug = _parse_task_entry(t)
        key = slug or title.lower()
        by_key[key] = (diff, title, slug)

    for t in new_titles or []:
        diff, title, slug = _parse_task_entry(t)
        key = slug or title.lower()
        old_title_key = title.lower()
        if slug and key not in by_key and old_title_key in by_key:
            old_diff, old_title, old_slug = by_key.pop(old_title_key)
            by_key[key] = (old_diff, old_title, old_slug)
        if key not in by_key:
            by_key[key] = (diff, title, slug)
        else:
            # upgrade UNKNOWN -> known
            old_diff, old_title, old_slug = by_key[key]
            if old_diff == "UNKNOWN" and diff in ("EASY", "MEDIUM", "HARD"):
                by_key[key] = (diff, title or old_title, slug or old_slug)
            elif not old_slug and slug:
                by_key[key] = (old_diff, old_title, slug)

    # stable output (alphabetical titles)
    merged = [
        _encode_task_entry(diff, title, slug)
        for diff, title, slug in sorted(by_key.values(), key=lambda x: x[1].lower())
    ]
    return merged


def update_snapshot_and_leaderboard(day_str: str, tid: int, solved_count: int, titles: List[str]):
    """
    Saves daily snapshot for (day, user) and updates all-time leaderboard points.
    IMPORTANT: This function is designed to be called multiple times per day (live updates).
    It merges today's tasks monotonically to avoid negative deltas caused by LeetCode recent list limits.
    """
    reset_day = _get_leaderboard_reset_day()
    count_for_leaderboard = (not reset_day) or (day_str >= reset_day)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT titles_json FROM daily_stats WHERE day=? AND telegram_id=?", (day_str, int(tid)))
    row = cur.fetchone()
    try:
        old_titles = json.loads(row[0]) if row and row[0] else []
        if not isinstance(old_titles, list):
            old_titles = []
    except Exception:
        old_titles = []

    merged_titles = _merge_titles(old_titles, titles or [])

    old_pts = points_from_titles(old_titles)
    new_pts = points_from_titles(merged_titles)
    delta = new_pts - old_pts

    if count_for_leaderboard and delta > 0:
        cur.execute(
            "INSERT INTO leaderboard(telegram_id, points) VALUES(?, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET points = points + ?",
            (int(tid), int(delta), int(delta)),
        )

    # keep solved_count consistent with merged titles
    solved_count_final = max(int(solved_count or 0), len(merged_titles))

    cur.execute(
        "REPLACE INTO daily_stats(day, telegram_id, solved_count, titles_json) VALUES(?,?,?,?)",
        (day_str, int(tid), int(solved_count_final), json.dumps(merged_titles, ensure_ascii=False)),
    )

    conn.commit()
    conn.close()
def clear_leaderboard_and_season_from(day_str: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("REPLACE INTO config(key,value) VALUES(?,?)", ("leaderboard_reset_day", str(day_str)))
    cur.execute("DELETE FROM leaderboard")
    cur.execute("DELETE FROM daily_stats WHERE day >= ?", (str(day_str),))
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


def leetcode_recent_accepted_submissions(nick: str):
    q = """
    query recentAccepted($username: String!, $limit: Int!) {
      recentAcSubmissionList(username: $username, limit: $limit) {
        title
        titleSlug
        timestamp
      }
    }
    """
    last_err = None
    for _attempt in range(3):
        try:
            resp = requests.post(
                LEETCODE_GRAPHQL,
                json={
                    "query": q,
                    "variables": {"username": nick, "limit": LEETCODE_RECENT_ACCEPTED_LIMIT},
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise RuntimeError(data["errors"])
            return data.get("data", {}).get("recentAcSubmissionList") or []
        except Exception as e:
            last_err = e
    raise last_err



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
        subs = leetcode_recent_accepted_submissions(nick)
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

        title = item.get("title") or "Unknown"
        slug = item.get("titleSlug") or None
        key = slug or title
        if key not in seen:
            seen.add(key)
            diff = leetcode_question_difficulty(slug)
            titles.append(_encode_task_entry(diff, title, slug))

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


def _get_daily_time_from_config() -> Tuple[int, int]:
    """Return (hour, minute) for daily report. Config overrides env defaults."""
    h_raw = db_get_config("daily_hour")
    m_raw = db_get_config("daily_minute")
    try:
        h = int(h_raw) if h_raw is not None else DAILY_HOUR
    except Exception:
        h = DAILY_HOUR
    try:
        m = int(m_raw) if m_raw is not None else DAILY_MINUTE
    except Exception:
        m = DAILY_MINUTE
    # clamp
    if h < 0 or h > 23:
        h = DAILY_HOUR
    if m < 0 or m > 59:
        m = DAILY_MINUTE
    return h, m

async def maybe_set_group_chat(update: Update):
    """
    Keep this hook for backward compatibility with handlers that call it.
    Report chat/topic is configured explicitly by /setgroup.
    """
    return


def _get_report_thread_id() -> Optional[int]:
    raw = db_get_config("report_message_thread_id")
    if not raw:
        return None
    try:
        return int(raw)
    except Exception:
        return None


async def send_report_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    thread_id = _get_report_thread_id()
    kwargs = {"chat_id": chat_id, "text": text}
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    await context.bot.send_message(**kwargs)


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
    /backup — admin-only: sends JSON backup (users/daily_stats/config/leaderboard).
    Use it with /restore (owner-only).
    """
    if not await _is_admin(update, context):
        await update.message.reply_text("⛔ Эта команда только для админа.")
        return

    await update.message.reply_text("🧳 Собираю бэкап в JSON… ща прилетит 📦")

    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("SELECT telegram_id, username, leetcode_nick FROM users")
        users_rows = [
            {"telegram_id": int(tid), "username": str(uname or ""), "leetcode_nick": str(nick or "")}
            for tid, uname, nick in cur.fetchall()
        ]

        cur.execute("SELECT day, telegram_id, solved_count, titles_json FROM daily_stats")
        stats_rows = []
        for day, tid, cnt, titles_json in cur.fetchall():
            stats_rows.append(
                {"day": str(day), "telegram_id": int(tid), "solved_count": int(cnt or 0), "titles_json": titles_json or "[]"}
            )

        cur.execute("SELECT key, value FROM config")
        config_rows = {str(k): str(v) for k, v in cur.fetchall()}

        cur.execute("SELECT telegram_id, points FROM leaderboard")
        leaderboard_rows = [{"telegram_id": int(tid), "points": int(pts or 0)} for tid, pts in cur.fetchall()]

        cur.execute("SELECT telegram_id, count FROM warns")
        warns_rows = [{"telegram_id": int(tid), "count": int(c or 0)} for tid, c in cur.fetchall()]

        conn.close()

        data = {
            "schema_version": DB_SCHEMA_VERSION,
            "exported_at": _now_str(),
            "tables": {
                "users": users_rows,
                "daily_stats": stats_rows,
                "config": config_rows,
                "leaderboard": leaderboard_rows,
                "warns": warns_rows,
            },
        }

        out_path = os.path.join(os.path.dirname(DB_PATH) or ".", "backup.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        with open(out_path, "rb") as f:
            await update.message.reply_document(document=f, filename="backup.json", caption="✅ Вот JSON-бэкап (включая лидерборд).")

    except Exception as e:
        logger.exception("Backup failed: %s", e)
        await update.message.reply_text(f"⚠️ Не смог сделать/отправить бэкап: {e}")


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
    leaderboard_rows = data.get("leaderboard")
    warns_rows = data.get("warns")

    if isinstance(data.get("tables"), dict):
        t = data["tables"]
        users_rows = users_rows or t.get("users")
        stats_rows = stats_rows or t.get("daily_stats")
        config_rows = config_rows or t.get("config")
        leaderboard_rows = leaderboard_rows or t.get("leaderboard")
        warns_rows = warns_rows or t.get("warns")

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
        cur.execute("DELETE FROM leaderboard")
        cur.execute("DELETE FROM warns")

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


        # Restore leaderboard (optional). If missing in backup, rebuild from daily_stats.
        restored_lb = False
        if leaderboard_rows:
            try:
                for row in leaderboard_rows:
                    tid = row.get("telegram_id") or row.get("tid")
                    pts = row.get("points") or row.get("score") or 0
                    if tid is None:
                        continue
                    cur.execute(
                        "REPLACE INTO leaderboard(telegram_id, points) VALUES(?,?)",
                        (int(tid), int(pts)),
                    )
                restored_lb = True
            except Exception:
                restored_lb = False

        # Restore warns (optional)
        if warns_rows:
            for row in warns_rows:
                tid = row.get("telegram_id") or row.get("tid")
                c = row.get("count") or row.get("warns") or 0
                if tid is None:
                    continue
                cur.execute(
                    "REPLACE INTO warns(telegram_id, count) VALUES(?,?)",
                    (int(tid), int(c)),
                )

        conn.commit()
        conn.close()

        if not restored_lb:
            recompute_leaderboard_from_daily_stats()

        _cache.clear()

        await update.message.reply_text(
            "✅ Восстановление завершено!"
            f"• users: {users_count}"
            f"• daily_stats: {stats_count}"
            "Можно продолжать: /list, /leaderboard."
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
            f"🔥 Готово! Ты зарегистрирован как: {nick}\n"
            "⚠️ Но LeetCode сейчас не ответил — позже /check покажет всё нормально."
        )
        return

    cnt = len(titles or [])
    mark = "✅" if cnt >= 1 else "❌"
    await update.message.reply_text(
        f"🔥 Готово! Ты зарегистрирован как: {nick}\n"
        f"Сегодня у тебя: {cnt} задач {mark}\n"
        "Теперь ты в общем списке /list (если вдруг не видно — сделай /list ещё раз через 5–10 сек 😄)"
    )


async def unregister(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    remove_user(update.effective_user.id)
    await update.message.reply_text("🫡 Удалил. Но я верю, ты вернёшься сильнее.")


async def setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Команда /setgroup должна быть вызвана в группе.")
        return

    member = await chat.get_member(user.id)
    if member.status not in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
        await update.message.reply_text("Только админ может назначить группу 😄")
        return

    db_set_config("report_chat_id", str(chat.id))
    thread_id = getattr(msg, "message_thread_id", None)
    if thread_id is not None:
        db_set_config("report_message_thread_id", str(thread_id))
        await update.message.reply_text("📌 Ок! Напоминания/отчёты будут приходить в этот топик.")
    else:
        db_set_config("report_message_thread_id", "")
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
        target_tid = None
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

    # Live points update for registered Telegram users
    try:
        today_str = datetime.now(TZ).strftime("%Y-%m-%d")
        target_tid = None
        for tid, _, lnick in rows:
            if str(lnick).lower() == str(nick).lower():
                target_tid = int(tid)
                break
        if target_tid is not None:
            update_snapshot_and_leaderboard(today_str, int(target_tid), len(titles or []), titles or [])
    except Exception:
        pass

    titles = titles or []
    try:
        tid_live = None
        for tid0, _u0, n0 in rows:
            if str(n0).lower() == str(nick).lower():
                tid_live = int(tid0)
                break
        if tid_live is not None:
            update_snapshot_and_leaderboard(today, int(tid_live), len(titles), titles)
    except Exception:
        pass
    if not titles:
        await update.message.reply_text(f"😴 {target_display}, сегодня ({today}) пока 0 задач. Пора спасать статистику!")
        return

    msg = (
        f"🔥 {target_display}, сегодня ({today}) решено {len(titles)} задач:\n"
        + "\n".join([f"• {_format_task_entry(t)}" for t in titles])
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
        await update.message.reply_text("Список пуст. Кто первый: /register <nick> ")
        return

    # /list @user OR /list <leetcodeNick>
    if len(context.args) == 1:
        target = context.args[0].strip()
        nick = None
        display = target
        target_tid = None

        if target.startswith("@"):
            t = target.lower()
            for tid, uname, lnick in rows:
                if (mention(uname)).lower() == t:
                    nick = lnick
                    display = mention(uname)
                    target_tid = int(tid)
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

        # Live points update when target is a registered Telegram user
        if target_tid is not None:
            try:
                update_snapshot_and_leaderboard(today, int(target_tid), len(titles or []), titles or [])
            except Exception:
                pass

        if not titles:
            await update.message.reply_text(f"{display} — сегодня ({today}) 0 задач ❌")
            return

        msg = f"{display} — сегодня ({today}) решил {len(titles)} задач ✅:\n" + "\n".join(
            [f"• {_format_task_entry(t)}" for t in titles]
        )
        await update.message.reply_text(msg)
        return

    # /list - summary
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    header = f"📋 Сегодняшний статус — {today_str}\n(цель: ≥1 задача)\n"

    scored = []  # (cnt, name, line, had_error)
    for tid, uname, nick in rows:
        titles, err = accepted_titles_today(nick)
        if err:
            name = mention(uname)
            scored.append((-1, name, f"{name} — ❓ ошибка проверки", True))
            continue

        cnt = len(titles or [])
        try:
            update_snapshot_and_leaderboard(today_str, int(tid), cnt, titles or [])
        except Exception:
            pass
        mark = "✅" if cnt >= 1 else "❌"
        name = mention(uname)
        scored.append((cnt, name, f"{name} — {cnt} задач {mark}", False))

    # Sort: more solved -> top; zeros -> bottom; errors -> very bottom
    scored.sort(key=lambda x: (x[3], -x[0], x[1].lower()))

    lines = [line for _, __, line, ___ in scored]
    await update.message.reply_text(header + "\n" + "\n".join(lines))


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    rows = list_users()
    if not rows:
        await update.message.reply_text("Пока нет зарегистрированных. /register <nick>")
        return

    points_map = get_leaderboard_points()
    entries = []
    for tid, uname, _nick in rows:
        entries.append((int(points_map.get(int(tid), 0)), mention(uname)))

    entries.sort(key=lambda x: (-x[0], x[1].lower()))
    top_pts = entries[0][0] if entries else 0

    lines = []
    for pts, name in entries:
        trophy = " 🥇" if pts == top_pts and pts > 0 else ""
        lines.append(f"{name}: {pts} балл(ов){trophy}")

    header = "🏆 Лидерборд (все время)\n(EASY=1, MEDIUM=3, HARD=5)\n"
    await update.message.reply_text(header + "\n".join(lines))


async def listtask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    rows = list_users()
    if not rows:
        await update.message.reply_text("Список пуст. Кто первый: /register <nick> 😄")
        return

    if len(context.args) != 1 or not context.args[0].strip().startswith("@"):
        await update.message.reply_text("Использование: /listtask @username")
        return

    target = context.args[0].strip().lower()
    day = datetime.now(TZ).date()
    day_str = day.strftime("%Y-%m-%d")

    header = f"📋 Сегодняшний статус — {day_str}\n(цель: ≥1 задача)\n"
    items = []

    for tid, uname, nick in rows:
        titles, err = accepted_titles_on_day(nick, day)
        name = mention(uname)
        if err:
            items.append((True, -1, name, []))
            continue

        titles = titles or []
        cnt = len(titles)
        mark = "✅" if cnt >= 1 else "❌"
        update_snapshot_and_leaderboard(day_str, int(tid), cnt, titles)
        items.append((False, cnt, f"{name} — {cnt} задач {mark}", titles))

    items.sort(key=lambda x: (x[0], -x[1], x[2].lower()))

    lines = []
    for had_error, _cnt, line, titles in items:
        if had_error:
            lines.append(f"{line} — ❓ ошибка проверки")
            continue
        lines.append(line)
        name_only = line.split(" — ", 1)[0].strip().lower()
        if name_only == target:
            if titles:
                lines.append("   └ решённые задачи:")
                for t in titles:
                    lines.append(f"      • {_format_task_entry(t)}")
            else:
                lines.append("   └ решённых задач сегодня нет.")

    await update.message.reply_text(header + "\n" + "\n".join(lines))


async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    if not await _is_admin(update, context):
        await update.message.reply_text("⛔ Эта команда только для админа.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Использование: /removeuser @username  ИЛИ  /removeuser <leetcode_nick>")
        return

    target = context.args[0].strip()
    rows = list_users()

    target_tid = None
    target_uname = None

    if target.startswith("@"):
        t = target.lower()
        for tid, uname, _ in rows:
            if mention(uname).lower() == t:
                target_tid = int(tid)
                target_uname = mention(uname)
                break
    else:
        for tid, uname, lnick in rows:
            if str(lnick).lower() == target.lower():
                target_tid = int(tid)
                target_uname = mention(uname)
                break

    if target_tid is None:
        await update.message.reply_text("Не нашёл пользователя в базе.")
        return

    remove_user(target_tid)
    recompute_leaderboard_from_daily_stats()
    await update.message.reply_text(f"✅ Удалил {target_uname} из бота.")


async def who(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    if len(context.args) != 1 or not context.args[0].strip().startswith("@"):
        await update.message.reply_text("Использование: /who @username")
        return

    target = context.args[0].strip().lower()
    rows = list_users()
    for _tid, uname, nick in rows:
        if mention(uname).lower() == target:
            await update.message.reply_text(f"{mention(uname)} → https://leetcode.com/u/{nick}/")
            return

    await update.message.reply_text("Не нашёл пользователя в базе. Пусть он сделает /register <nick>.")



async def settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    user = update.effective_user
    if not user or not OWNER_ID or user.id != OWNER_ID:
        await update.message.reply_text("⛔ Нет доступа.")
        return

    if len(context.args) != 1 or ":" not in context.args[0]:
        await update.message.reply_text("Использование: /settime HH:MM  (например: /settime 23:30)")
        return

    raw = context.args[0].strip()
    try:
        hh_s, mm_s = raw.split(":", 1)
        hh = int(hh_s)
        mm = int(mm_s)
    except Exception:
        await update.message.reply_text("Неверный формат. Использование: /settime HH:MM")
        return

    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        await update.message.reply_text("Неверное время. Часы 0-23, минуты 0-59.")
        return

    db_set_config("daily_hour", str(hh))
    db_set_config("daily_minute", str(mm))

    # reschedule daily job (name-based)
    try:
        jq = context.application.job_queue
        for j in jq.get_jobs_by_name("daily_report"):
            j.schedule_removal()
        jq.run_daily(
            daily_report_job,
            time=time(hour=hh, minute=mm, tzinfo=TZ),
            name="daily_report",
        )
        await update.message.reply_text(f"✅ Время отчёта обновлено: {hh:02d}:{mm:02d} (Asia/Almaty)")
    except Exception as e:
        logger.exception("settime reschedule failed: %s", e)
        await update.message.reply_text(f"⚠️ Время сохранено, но job не пересоздался: {e}")


async def unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    if not await _is_admin(update, context):
        await update.message.reply_text("⛔ Эта команда только для админа.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Использование: /unwarn @username  ИЛИ  /unwarn <leetcode_nick>  ИЛИ  /unwarn all")
        return

    target = context.args[0].strip()
    if target.lower() in ("all", "все", "everyone"):
        clear_all_warns()
        await update.message.reply_text("✅ Предупреждения сброшены для всех участников.")
        return

    rows = list_users()

    target_tid = None
    target_name = None

    if target.startswith("@"):
        t = target.lower()
        for tid, uname, _ in rows:
            if mention(uname).lower() == t:
                target_tid = int(tid)
                target_name = mention(uname)
                break
    else:
        for tid, uname, nick in rows:
            if str(nick).lower() == target.lower():
                target_tid = int(tid)
                target_name = mention(uname)
                break

    if target_tid is None:
        await update.message.reply_text("Не нашёл пользователя в базе.")
        return

    clear_warns(target_tid)
    await update.message.reply_text(f"✅ Предупреждения сброшены для {target_name}.")

async def clearboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    user = update.effective_user
    if not user or not OWNER_ID or user.id != OWNER_ID:
        await update.message.reply_text("⛔ Нет доступа.")
        return

    day_str = datetime.now(TZ).date().strftime("%Y-%m-%d")
    clear_leaderboard_and_season_from(day_str)
    _cache.clear()
    await update.message.reply_text("✅ Лидерборд очищен. Новый сезон начался с сегодняшнего дня.")


async def recalculate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    if not await _is_admin(update, context):
        await update.message.reply_text("⛔ Эта команда только для админа.")
        return

    recompute_leaderboard_from_daily_stats()
    await update.message.reply_text("✅ Лидерборд пересчитан из сохранённых daily_stats.")


async def recheckday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    if not await _is_admin(update, context):
        await update.message.reply_text("⛔ Эта команда только для админа.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Использование: /recheckday YYYY-MM-DD")
        return

    try:
        target_day = datetime.strptime(context.args[0].strip(), "%Y-%m-%d").date()
    except Exception:
        await update.message.reply_text("Неверная дата. Формат: YYYY-MM-DD")
        return

    rows = list_users()
    if not rows:
        await update.message.reply_text("Пока нет зарегистрированных пользователей.")
        return

    day_str = target_day.strftime("%Y-%m-%d")
    ok = 0
    errors = []

    for tid, uname, nick in rows:
        titles, err = accepted_titles_on_day(nick, target_day)
        if err:
            errors.append(mention(uname))
            continue
        update_snapshot_and_leaderboard(day_str, int(tid), len(titles or []), titles or [])
        ok += 1

    recompute_leaderboard_from_daily_stats()
    _cache.clear()

    msg = f"✅ Перепроверил {day_str}: обновлено {ok}/{len(rows)} пользователей."
    if errors:
        msg += "\n⚠️ Ошибка LeetCode у: " + ", ".join(errors)
    await update.message.reply_text(msg)


async def _week_disabled(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    msg_lines = ["📊 Статистика за неделю (последние 7 дней\n"]
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
    for tid, uname, nick in rows:
        titles, err = accepted_titles_on_day(nick, today)
        if err:
            continue
        try:
            update_snapshot_and_leaderboard(today_str, int(tid), len(titles or []), titles or [])
        except Exception:
            pass
        if not titles:
            not_done.append(mention(uname))

    # Everyone done -> celebrate once/day
    if not not_done:
        flag_key = f"all_done_{today_str}"
        if not db_get_config(flag_key):
            await send_report_message(
                context,
                chat_id,
                (
                    "🎉 ВСЕ МОЛОДЦЫ! \n"
                    "Каждый решил минимум 1 задачу сегодня 💪🔥"
                    "Группа официально НЕ ленивая 😎"
                ),
            )
            db_set_config(flag_key, "1")
        return

    # Still slackers -> fun ping
    emojis = ["⏰", "🚨", "👀", "🧠", "🔥"]
    e = emojis[int(datetime.now(TZ).timestamp()) % len(emojis)]
    await send_report_message(
        context,
        chat_id,
        (
            f"{e} Напоминалка: сегодня ещё без задач:"
            + ", ".join(not_done)
            + " \nПравило простое: минимум 1 задача. Погнали! 🚀"
        ),
    )


async def daily_report_job(
    context: ContextTypes.DEFAULT_TYPE,
    target_day: Optional[date] = None,
    apply_warns: bool = True,
):
    chat_id = db_get_config("report_chat_id")
    if not chat_id:
        return
    chat_id = int(chat_id)

    rows = list_users()
    if not rows:
        await send_report_message(context, chat_id, "Сегодня никого не было в списке 😄")
        return

    day = target_day or datetime.now(TZ).date()
    day_str = day.strftime("%Y-%m-%d")
    warns_key = f"warns_applied_{day_str}"
    should_apply_warns = apply_warns and not bool(db_get_config(warns_key))

    items = []  # (had_error, cnt, name)
    mvp_max = -1
    mvp_winners: List[str] = []

    for tid, uname, nick in rows:
        titles, err = accepted_titles_on_day(nick, day)
        name = mention(uname)

        if err:
            items.append((True, -1, name))
            continue

        titles = titles or []
        cnt = len(titles)

        # Warn system: only if LeetCode check succeeded (no err) and user solved 0 tasks today.
        # For catch-up reports (yesterday), logic is the same. We don't warn on LC errors above.
        warn_count = None
        kicked = False
        if should_apply_warns and cnt == 0:
            warn_count = inc_warn(int(tid))
            if warn_count >= 3:
                # Kick from group (ban+unban), requires bot admin rights.
                try:
                    await context.bot.ban_chat_member(chat_id=chat_id, user_id=int(tid))
                    await context.bot.unban_chat_member(chat_id=chat_id, user_id=int(tid))
                    kicked = True
                except Exception as e:
                    logger.warning("Kick failed for %s (%s): %s", name, tid, e)

        update_snapshot_and_leaderboard(day_str, int(tid), cnt, titles)

        items.append((False, cnt, name))

        if cnt > mvp_max:
            mvp_max = cnt
            mvp_winners = [name]
        elif cnt == mvp_max and cnt > 0:
            mvp_winners.append(name)

    items.sort(key=lambda x: (x[0], -x[1], x[2].lower()))

    # Build a map name -> warn_count for display (warns are tracked by telegram_id; name is stable in this report)
    # We only show warn counts for users who have 0 solved and no LC error.
    get_warn_count_by_name: Dict[str, int] = {}
    try:
        for tid, uname, _nick in rows:
            nm = mention(uname)
            get_warn_count_by_name[nm] = get_warn_count(int(tid))
    except Exception:
        get_warn_count_by_name = {}

    report_lines = []
    for had_error, cnt, name in items:
        if had_error:
            report_lines.append(f"{name} — ❓ ошибка проверки (LeetCode недоступен)")
        else:
            mark = "✅" if cnt >= 1 else "❌"
            if cnt == 0:
                w = get_warn_count_by_name.get(name, None)
                if (not should_apply_warns) or w is None:
                    # fallback: do not show
                    report_lines.append(f"{name} — {cnt} задач {mark}")
                else:
                    suffix = f" ⚠️ warn {w}/3"
                    if w >= 3:
                        suffix += " ❌ KICK"
                    report_lines.append(f"{name} — {cnt} задач {mark}{suffix}")
            else:
                report_lines.append(f"{name} — {cnt} задач {mark}")

    if mvp_max <= 0:
        mvp_line = "🏆 MVP дня: сегодня без победителей… но завтра новый шанс 😄"
    else:
        winners = ", ".join(mvp_winners)
        mvp_line = f"🏆 MVP дня: {winners} — {mvp_max} задач(и) 🔥"

    header = f"🧾 Итог дня — {day_str}\n(цель: ≥1 задача)\n"
    text = header + "\n".join(report_lines) + "\n\n" + mvp_line
    await send_report_message(context, chat_id, text)
    if should_apply_warns:
        db_set_config(warns_key, "1")
    _set_last_report_day(day_str)
async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_set_group_chat(update)
    text = "ℹ️ *Информация о боте*\n\n Я слежу за тем, чтобы каждый решал *минимум 1 задачу в день* на LeetCode 💪\n\n *Как начать:*\n 1️⃣Каждый участник пишет /register <leetcode_nick>\n\n *Команды:*\n • /register <nick> — зарегистрировать LeetCode ник\n • /unregister — удалить себя из бота\n • /check — сколько и какие задачи *ты* решил сегодня\n • /list — статус всех за сегодня (кол-во + ✅/❌)\n • /list @user — какие задачи решил пользователь сегодня\n • /week — статистика за последние 7 дней\n • /week @user — статистика за 7 дней для конкретного пользователя\n • /info — эта справка\n\n *Админ-команды:*\n • /unwarn @user — сбросить предупреждения одному участнику\n • /unwarn all — сбросить предупреждения всем\n\n *Авто-логика:*\n ⏰ Каждые 3 часа бот пингует тех, кто ещё не решил ни одной задачи\n 🎉 Как только *все* решат ≥1 задачу — бот поздравит группу\n 🏆 В конце дня бот отправляет отчёт + MVP дня\n\n Правило простое: *1 задача в день — и ты красавчик* 😎"
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
        await daily_report_job(context, target_day=y, apply_warns=False)
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
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("listtask", listtask))
    app.add_handler(CommandHandler("removeuser", removeuser_cmd))
    app.add_handler(CommandHandler("who", who))
    app.add_handler(CommandHandler("clearboard", clearboard))
    app.add_handler(CommandHandler("recalculate", recalculate))
    app.add_handler(CommandHandler("recheckday", recheckday))
    app.add_handler(CommandHandler("settime", settime))  # owner-only
    app.add_handler(CommandHandler("unwarn", unwarn))  # owner-only
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CommandHandler("backup", backup))
    app.add_handler(CommandHandler("restore", restore))  # hidden owner-only

    app.job_queue.run_repeating(reminder_job, interval=REMINDER_INTERVAL_SECONDS, first=10)

    # End-of-day report (and stats snapshot)
    h, m = _get_daily_time_from_config()
    app.job_queue.run_daily(
        daily_report_job,
        time=time(hour=h, minute=m, tzinfo=TZ),
        name="daily_report",
    )

    # Catch-up once shortly after start (если бот был оффлайн в момент отчёта)
    app.job_queue.run_once(catchup_job, when=30)

    print("Bot started. Press Ctrl-C to stop.")
    app.run_polling()



if __name__ == "__main__":
    main()
