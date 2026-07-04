# LeetCode Telegram Bot

Telegram bot for a group challenge: every participant must solve at least one LeetCode problem per day.

## Features

- `/register <leetcode_nick>` links a Telegram user to a LeetCode profile.
- `/check` shows your accepted problems for today.
- `/check @username` or `/list @username` shows another registered user's accepted problems for today.
- `/who @username` returns the user's LeetCode profile URL.
- `/leaderboard` ranks users by points: Easy = 1, Medium = 3, Hard = 5.
- `/recalculate` lets admins rebuild leaderboard points from saved daily snapshots.
- `/recheckday YYYY-MM-DD` lets admins refetch a specific day and then rebuild the leaderboard.
- Daily report marks who solved at least one problem and applies warnings.
- Users with 3 warnings are removed from the configured group, if the bot has admin rights.
- `/backup` and `/restore` help move the bot between servers.

## Why scoring is more reliable now

The bot uses LeetCode's accepted-submissions query with an explicit limit instead of the smaller mixed recent-submissions list. This avoids missing accepted problems when a user has many failed submissions or many attempts in one day.

Daily warning application is also guarded per date, so a duplicate daily report cannot give the same user multiple warnings for the same missed day.

## Local Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env`, then export it before running:

```bash
set -a
source .env
set +a
python telegram_bot.py
```

## Required Telegram Setup

1. Create a bot through BotFather and copy `TELEGRAM_TOKEN`.
2. Add the bot to your Telegram group.
3. Give the bot admin rights if you want automatic removal after 3 warnings.
4. Run `/setgroup` inside the target group.
5. Each participant runs `/register <leetcode_nick>`.

## Hosting Notes

This bot uses polling, so it does not need a public HTTPS webhook URL. It can run on a VPS, Oracle Cloud Always Free, Railway, Render, Fly.io, or any always-on machine.

For free hosting, Oracle Cloud Always Free is usually the most stable long-term option, but setup is more manual. A small paid VPS is simpler and more predictable.
