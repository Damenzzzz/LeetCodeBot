import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime


class TelegramBotDbSmokeTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="leetcode_bot_test_", suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        os.environ["DB_PATH"] = self.db_path
        os.environ["DATABASE_URL"] = ""
        sys.modules.pop("telegram_bot", None)
        self.bot = importlib.import_module("telegram_bot")
        self.bot.init_db()

    def tearDown(self):
        sys.modules.pop("telegram_bot", None)
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def test_snapshot_leaderboard_and_backup_tables(self):
        self.bot.add_user(1, "alice", "alice_lc")
        today = datetime.now(self.bot.TZ).strftime("%Y-%m-%d")
        titles = [self.bot._encode_task_entry("EASY", "Two Sum", "two-sum")]

        merged = self.bot.update_snapshot_and_leaderboard(today, 1, 1, titles)

        self.assertEqual(merged, titles)
        self.assertEqual(self.bot.get_leaderboard_points()[1], 1)
        snapshot = self.bot.get_daily_snapshot(today, 1, 3600)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["titles"], titles)
        self.assertGreater(snapshot["fetched_at"], 0)

        self.bot.set_cached_problem_difficulty("two-sum", "EASY")
        backup_tables = self.bot.collect_backup_data()["tables"]
        self.assertTrue(backup_tables["users"])
        self.assertTrue(backup_tables["daily_stats"])
        self.assertTrue(backup_tables["leaderboard"])
        self.assertTrue(backup_tables["problem_cache"])

    def test_warning_is_awarded_once_per_day_and_backed_up(self):
        self.bot.add_user(1, "alice", "alice_lc")
        today = datetime.now(self.bot.TZ).strftime("%Y-%m-%d")

        warn_count, awarded = self.bot.award_warn_once(today, 1)
        warn_count_again, awarded_again = self.bot.award_warn_once(today, 1)

        self.assertTrue(awarded)
        self.assertEqual(warn_count, 1)
        self.assertFalse(awarded_again)
        self.assertEqual(warn_count_again, 1)
        backup_tables = self.bot.collect_backup_data()["tables"]
        self.assertEqual(backup_tables["warns"][0]["count"], 1)
        self.assertEqual(backup_tables["warn_events"][0]["day"], today)

    def test_migrates_v5_daily_stats_without_losing_rows(self):
        sys.modules.pop("telegram_bot", None)
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute(
            """
            CREATE TABLE daily_stats (
                day TEXT,
                telegram_id BIGINT,
                solved_count INTEGER,
                titles_json TEXT,
                PRIMARY KEY(day, telegram_id)
            )
            """
        )
        cur.execute("INSERT INTO config(key, value) VALUES('db_schema_version', '5')")
        cur.execute(
            "INSERT INTO daily_stats(day, telegram_id, solved_count, titles_json) VALUES(?, ?, ?, ?)",
            ("2026-07-13", 1, 1, "[]"),
        )
        conn.commit()
        conn.close()

        self.bot = importlib.import_module("telegram_bot")
        self.bot.init_db()

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(daily_stats)")
        columns = {row[1] for row in cur.fetchall()}
        cur.execute("SELECT solved_count FROM daily_stats WHERE day='2026-07-13' AND telegram_id=1")
        row = cur.fetchone()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='problem_cache'")
        problem_cache_exists = cur.fetchone() is not None
        conn.close()

        self.assertIn("fetched_at", columns)
        self.assertEqual(row[0], 1)
        self.assertTrue(problem_cache_exists)


if __name__ == "__main__":
    unittest.main()
