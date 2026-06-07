# Deployment Guide — Telegram + Railway Cron

End-to-end steps to get the scanner running daily on Railway with Telegram
delivery. Walk through these in order.

---

## Step 1 — Create your Telegram bot

1. Open Telegram, search for **@BotFather**, start a chat
2. Send `/newbot`
3. Choose a name (display name) and a username (must end in `_bot`)
4. BotFather replies with a token like `7123456789:AAFxxxxxxxxxxxxxxxxxxxxxxxxx`
5. **Save this token** — you'll need it in Step 4

## Step 2 — Find your admin chat_id

1. Search for your new bot on Telegram, start a chat (send `/start` or any message)
2. In a browser, open:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
3. You'll see JSON with `"from":{"id":123456789, ...}` — that number is your chat_id
4. **Save it** for Step 4

## Step 3 — Apply the schema

Run in Railway's Postgres web SQL console (or psql):

```sql
-- contents of schema_telegram.sql
```

## Step 4 — Update local `.env`

Edit your local `.env` and add:

```bash
TELEGRAM_BOT_TOKEN=7123456789:AAFxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_ADMIN_CHAT_ID=123456789
```

(Don't commit `.env`.)

## Step 5 — Add yourself as a subscriber

```bash
python seed_admin_subscriber.py 123456789 "Your Name" --tier admin
```

Replace `123456789` with your actual chat_id. Add other testers/admins the same way.

## Step 6 — Test locally

Test the dispatch flow without running the heavy scanner:

```bash
# First, generate a scan report
python market_scanner.py

# Then test dispatch dry-run
python telegram_dispatch.py --dry-run

# If that looks good, send for real
python telegram_dispatch.py
```

You should receive the scan_result_*.txt as a file attachment on Telegram.

## Step 7 — Test the wrapper

```bash
python run_daily.py --skip-dispatch   # local sanity, no Telegram
python run_daily.py                   # full flow
```

This should:
- Run scanner
- Send via Telegram
- Log to `job_runs` table

## Step 8 — Push to GitHub

```bash
git add .
git commit -m "Add Telegram dispatch and Railway cron setup"
git push origin main
```

## Step 9 — Configure Railway

### 9a. Create services (one per cron schedule)

In Railway dashboard:
1. Open your project
2. **New** → **GitHub Repo** → select your repo (if not already linked)
3. Railway auto-detects Python and starts building

You need **three cron services**, each pointing to the same repo but with
different start commands and schedules:

### Service 1: Daily Pipeline (7 PM IST)

| Setting | Value |
|---|---|
| Start command | `python run_daily.py` |
| Cron schedule | `30 13 * * *` |
| Timezone | UTC (the schedule is UTC; 13:30 UTC = 7:00 PM IST) |
| Env vars | `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_CHAT_ID` |

### Service 2: Recovery Pipeline (5 AM IST — safety net)

| Setting | Value |
|---|---|
| Start command | `python run_daily.py --recovery` |
| Cron schedule | `30 23 * * *` |
| Timezone | UTC (23:30 UTC = 5:00 AM IST next day) |
| Env vars | Same as above |

This service runs every day at 5 AM IST. It checks `job_runs` — if today's
DAILY_PIPELINE already has SUCCESS status, it exits without doing anything.
If not, it re-runs the full pipeline.

### Service 3: Weekly Maintenance (Sundays 6 AM IST)

| Setting | Value |
|---|---|
| Start command | `python run_weekly_maintenance.py` |
| Cron schedule | `30 0 * * 0` |
| Timezone | UTC (0:30 UTC Sunday = 6:00 AM IST Sunday) |
| Env vars | Same as above |

Runs F&O list sync + corp action refresh once a week on Sundays.

### 9b. Set environment variables

For EACH service, under Variables:

- `DATABASE_URL` → use the **internal** Railway Postgres URL (the `.railway.internal` one)
- `TELEGRAM_BOT_TOKEN` → your bot token
- `TELEGRAM_ADMIN_CHAT_ID` → your admin chat_id (or comma-separated list)

### 9c. Verify deployment

Watch the logs after the first scheduled run. You should see:
- Connection successful
- Scanner completed (X stocks)
- Telegram dispatch completed (X subscribers)
- Job logged to `job_runs` with status SUCCESS

## Step 10 — Verify error alerts work

Temporarily break something to test the error path:

```bash
# Locally, set a bad DATABASE_URL in .env and run:
python run_daily.py
```

You should receive a Telegram alert at your admin chat_id within seconds.

Then restore the correct DATABASE_URL.

---

## Going forward

Any code change you push to GitHub `main` branch will auto-deploy to Railway.

The next scheduled cron will use the new code. No manual restart needed.

**Important:** Changes that affect schema (new tables, columns) need to be
applied via Railway's SQL console BEFORE pushing the code that uses them.

---

## Troubleshooting

**Bot doesn't respond to /start:**
- Bots only PUSH messages by default. They don't auto-listen.
- The current architecture is push-only — you message the bot first so they
  get your chat_id, then you add the chat_id via `seed_admin_subscriber.py`.
- Interactive listening is a Phase 2 feature.

**"Failed to send: 403 bot_blocked":**
- A user has blocked your bot. Their `last_status` will show `bot_blocked`.
- Consider auto-setting `opted_in = FALSE` for these users (TODO).

**Cron not firing on Railway:**
- Verify the schedule string is in UTC, not IST
- Check Railway logs — service might be failing to start (env vars missing)
- Railway free tier sometimes has cold-start delays

**File not found error on Railway:**
- Railway containers reset between runs. The scan_result_*.txt file written
  by `run_daily.py` is lost when the next service starts.
- Since the dispatch runs INSIDE `run_daily.py` (same process), the file is
  still in memory/local filesystem when dispatch runs. This is fine.
- Standalone `python telegram_dispatch.py` calls on Railway WILL fail.
  Always use `run_daily.py` as the entry point on Railway.

**Want to see what's in job_runs:**
```sql
SELECT run_date, job_type, status, completed_at, error_message
FROM trade_scanner.job_runs
ORDER BY started_at DESC
LIMIT 20;
```

---

## Quick reference

| What | Where | Command |
|---|---|---|
| Add a subscriber | Local | `python seed_admin_subscriber.py CHAT_ID "Name"` |
| Pause a subscriber | DB | `UPDATE telegram_subscribers SET opted_in = FALSE WHERE chat_id = X;` |
| Check last run | DB | `SELECT * FROM job_runs ORDER BY started_at DESC LIMIT 5;` |
| Manually trigger | Railway | Click "Restart" on the service |
| Local test | Local | `python run_daily.py --skip-dispatch` |
