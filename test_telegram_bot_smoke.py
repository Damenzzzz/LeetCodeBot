import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
import asyncio


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

    def test_bulk_snapshot_and_warning_reads(self):
        self.bot.add_user(1, "alice", "alice_lc")
        self.bot.add_user(2, "bob", "bob_lc")
        today = datetime.now(self.bot.TZ).strftime("%Y-%m-%d")

        self.bot.update_snapshots_and_leaderboard(
            today,
            [
                (1, 1, [self.bot._encode_task_entry("EASY", "Two Sum", "two-sum")]),
                (2, 1, [self.bot._encode_task_entry("HARD", "N-Queens", "n-queens")]),
            ],
        )
        self.bot.set_warn_count(1, 2)

        snapshots = self.bot.get_daily_snapshots(today, [1, 2])
        warn_counts = self.bot.get_warn_counts([1, 2])

        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[1]["solved_count"], 1)
        self.assertEqual(snapshots[2]["titles"][0], "HARD N-Queens||n-queens")
        self.assertEqual(warn_counts, {1: 2})

    def test_group_leetcode_batch_reuses_single_response(self):
        today = datetime.now(self.bot.TZ).date()
        timestamp = str(int(datetime.now(self.bot.TZ).timestamp()))
        calls = []

        original_submissions = self.bot.leetcode_recent_accepted_submissions_batch
        original_difficulties = self.bot.leetcode_question_difficulties
        try:
            def fake_submissions(nicks):
                calls.append(list(nicks))
                return {
                    "alice_lc": [{"title": "Two Sum", "titleSlug": "two-sum", "timestamp": timestamp}],
                    "bob_lc": [{"title": "N-Queens", "titleSlug": "n-queens", "timestamp": timestamp}],
                }

            self.bot.leetcode_recent_accepted_submissions_batch = fake_submissions
            self.bot.leetcode_question_difficulties = lambda slugs: {
                "two-sum": "EASY",
                "n-queens": "HARD",
            }

            result = self.bot.accepted_titles_on_day_batch(["alice_lc", "bob_lc"], today)
        finally:
            self.bot.leetcode_recent_accepted_submissions_batch = original_submissions
            self.bot.leetcode_question_difficulties = original_difficulties

        self.assertEqual(calls, [["alice_lc", "bob_lc"]])
        self.assertEqual(result["alice_lc"][0], ["EASY Two Sum||two-sum"])
        self.assertEqual(result["bob_lc"][0], ["HARD N-Queens||n-queens"])

    def test_list_ranks_by_points_not_by_task_count(self):
        """4 Easy (4 points) must rank below 1 Hard (5 points)."""
        self.bot.add_user(1, "easyguy", "easy_lc")
        self.bot.add_user(2, "hardguy", "hard_lc")
        today = datetime.now(self.bot.TZ).strftime("%Y-%m-%d")

        easy_titles = [
            self.bot._encode_task_entry("EASY", f"Easy Task {i}", f"easy-task-{i}")
            for i in range(4)
        ]
        hard_titles = [self.bot._encode_task_entry("HARD", "N-Queens", "n-queens")]
        self.bot.update_snapshots_and_leaderboard(
            today, [(1, len(easy_titles), easy_titles), (2, len(hard_titles), hard_titles)]
        )

        self.assertEqual(self.bot.points_from_titles(easy_titles), 4)
        self.assertEqual(self.bot.points_from_titles(hard_titles), 5)

        sent = []

        class FakeMessage:
            async def reply_text(self, text, **kwargs):
                sent.append(text)

        update = SimpleNamespace(
            message=FakeMessage(),
            effective_chat=SimpleNamespace(id=1, type="private"),
            effective_user=SimpleNamespace(id=1, username="easyguy", full_name="Easy", is_bot=False),
        )
        context = SimpleNamespace(args=[], bot=None)

        asyncio.run(self.bot.listcmd(update, context))

        self.assertEqual(len(sent), 1)
        lines = [ln for ln in sent[0].splitlines() if "балл" in ln and "—" in ln]
        self.assertEqual(len(lines), 2)
        self.assertIn("hardguy", lines[0])
        self.assertIn("5 балл(ов) · 1 задач", lines[0])
        self.assertIn("easyguy", lines[1])
        self.assertIn("4 балл(ов) · 4 задач", lines[1])

    def test_membership_checks_are_cached_between_commands(self):
        class FakeBot:
            def __init__(self):
                self.calls = []

            async def get_chat_member(self, chat_id, user_id):
                self.calls.append((chat_id, user_id))
                return SimpleNamespace(status="member")

        fake_bot = FakeBot()
        context = SimpleNamespace(bot=fake_bot)
        rows = [(1, "alice", "alice_lc"), (2, "bob", "bob_lc")]
        self.bot._membership_cache.clear()

        async def run_checks():
            first = await self.bot.prune_inactive_users(context, -100, rows, "test")
            second = await self.bot.prune_inactive_users(context, -100, rows, "test")
            return first, second

        first, second = asyncio.run(run_checks())
        self.assertEqual(first, rows)
        self.assertEqual(second, rows)
        self.assertEqual(sorted(fake_bot.calls), [(-100, 1), (-100, 2)])


if __name__ == "__main__":
    unittest.main()
