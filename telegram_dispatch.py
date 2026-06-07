"""
Telegram Dispatch
─────────────────
Sends today's scan report to opted-in subscribers, filtered by tier.

Behavior:
  • Reads scan_result_DD_MMM_YYYY.txt from current dir
  • Reads corp_actions_DD_MMM_YYYY.txt if it exists (Monday only)
  • Queries telegram_subscribers WHERE opted_in = TRUE
  • Tier-based filtering:
      - 'paid' / 'admin' → receives full report as .txt attachment
      - 'free' → for now: also receives full report (until tier logic kicks in)
                 Future: free gets summary-only message
  • Logs send status back to telegram_subscribers.last_status
  • Updates last_message_at

Errors during send (user blocked bot, etc.) are caught per-user — one
failure doesn't abort the batch.

Usage:
    python telegram_dispatch.py                 # auto-detect today's file
    python telegram_dispatch.py --file path.txt # specify a file
    python telegram_dispatch.py --dry-run       # show what WOULD be sent
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path
import requests
from dotenv import load_dotenv

from db import test_connection, get_cursor

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN and "--dry-run" not in sys.argv:
    # Allow dry-run without a token for testing
    print("⚠️  TELEGRAM_BOT_TOKEN not set in .env — only --dry-run will work")

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None


# ─────────────────────────────────────────────
# TELEGRAM API HELPERS
# ─────────────────────────────────────────────
def send_text(chat_id, text, parse_mode=None):
    """Send a plain text message. Returns (ok, error_str)."""
    if API_BASE is None:
        return False, "no_bot_token"
    try:
        resp = requests.post(
            f"{API_BASE}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode or "",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            return True, None
        # 403 = user blocked bot
        if resp.status_code == 403:
            return False, "bot_blocked"
        return False, f"http_{resp.status_code}: {resp.text[:120]}"
    except Exception as e:
        return False, f"exception: {e}"


def send_document(chat_id, file_path, caption=""):
    """Send a file as a document. Returns (ok, error_str)."""
    if API_BASE is None:
        return False, "no_bot_token"
    if not Path(file_path).exists():
        return False, "file_not_found"
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                f"{API_BASE}/sendDocument",
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"document": (Path(file_path).name, f, "text/plain")},
                timeout=30,
            )
        if resp.status_code == 200:
            return True, None
        if resp.status_code == 403:
            return False, "bot_blocked"
        return False, f"http_{resp.status_code}: {resp.text[:120]}"
    except Exception as e:
        return False, f"exception: {e}"


# ─────────────────────────────────────────────
# SUBSCRIBER QUERIES
# ─────────────────────────────────────────────
def get_active_subscribers(tier_filter=None):
    """Returns list of (chat_id, username, tier, is_admin)."""
    with get_cursor() as (_, cur):
        sql = """
            SELECT chat_id, username, tier, is_admin
            FROM telegram_subscribers
            WHERE opted_in = TRUE
        """
        params = []
        if tier_filter:
            sql += " AND tier = ANY(%s)"
            params.append(tier_filter)
        sql += " ORDER BY is_admin DESC, tier, subscribed_at"
        cur.execute(sql, params)
        return cur.fetchall()


def get_admin_subscribers():
    """Returns admin chat_ids (for error alerts)."""
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT chat_id FROM telegram_subscribers
            WHERE is_admin = TRUE AND opted_in = TRUE
        """)
        return [r[0] for r in cur.fetchall()]


def update_send_status(chat_id, status):
    with get_cursor() as (_, cur):
        cur.execute("""
            UPDATE telegram_subscribers
            SET last_message_at = NOW(), last_status = %s
            WHERE chat_id = %s
        """, (status, chat_id))


# ─────────────────────────────────────────────
# REPORT FILE DETECTION
# ─────────────────────────────────────────────
def find_scan_file(file_arg=None):
    if file_arg:
        if not Path(file_arg).exists():
            print(f"❌ File not found: {file_arg}")
            return None
        return Path(file_arg)
    fname = f"scan_result_{datetime.now().strftime('%d_%b_%Y')}.txt"
    p = Path(fname)
    if not p.exists():
        # Try yesterday's file as fallback (in case of 5 AM recovery run)
        from datetime import timedelta
        yfname = f"scan_result_{(datetime.now() - timedelta(days=1)).strftime('%d_%b_%Y')}.txt"
        yp = Path(yfname)
        if yp.exists():
            print(f"   ℹ️  Today's file not found; using yesterday's: {yfname}")
            return yp
        print(f"❌ Scan file not found: {fname} (or yesterday's)")
        return None
    return p


def find_corp_action_file():
    fname = f"corp_actions_{datetime.now().strftime('%d_%b_%Y')}.txt"
    p = Path(fname)
    return p if p.exists() else None


# ─────────────────────────────────────────────
# ERROR ALERT (called from outer wrapper on failure)
# ─────────────────────────────────────────────
def send_error_alert_to_admins(error_text):
    """Best-effort alert. Uses env var directly so it works even if DB is down."""
    admin_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
    if not admin_id or not API_BASE:
        print(f"⚠️  Cannot send admin alert (missing token or admin id): {error_text}")
        return
    try:
        admin_ids = [int(x.strip()) for x in admin_id.split(",") if x.strip()]
    except Exception:
        admin_ids = []
    for aid in admin_ids:
        send_text(aid, f"🚨 Scanner job FAILED\n\n{error_text[:3500]}")


# ─────────────────────────────────────────────
# MAIN DISPATCH
# ─────────────────────────────────────────────
def main():
    dry_run  = "--dry-run" in sys.argv
    file_arg = None
    if "--file" in sys.argv:
        i = sys.argv.index("--file")
        if i + 1 < len(sys.argv):
            file_arg = sys.argv[i + 1]

    print("=" * 65)
    print("  TELEGRAM DISPATCH")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print(f"  Mode: {'DRY-RUN' if dry_run else 'LIVE'}")
    print("=" * 65)

    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        sys.exit(1)

    scan_file = find_scan_file(file_arg)
    if not scan_file:
        sys.exit(1)
    print(f"\n📄 Scan file: {scan_file.name} ({scan_file.stat().st_size} bytes)")

    corp_file = find_corp_action_file()
    if corp_file:
        print(f"📅 Corp action file: {corp_file.name} (Monday)")

    subscribers = get_active_subscribers()
    if not subscribers:
        print("\n⚠️  No active subscribers in telegram_subscribers table.")
        print("   Add yourself with: python seed_admin_subscriber.py")
        return

    print(f"\n👥 Active subscribers: {len(subscribers)}")
    if dry_run:
        for s in subscribers:
            chat_id, username, tier, is_admin = s
            label = "ADMIN" if is_admin else tier.upper()
            print(f"   {label:<6} | {chat_id:<15} | {username or 'no_username'}")
        print("\n   (dry-run — no messages sent)")
        return

    if not BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN missing in .env. Cannot send.")
        sys.exit(1)

    # Send to each subscriber
    sent, failed = 0, 0
    for s in subscribers:
        chat_id, username, tier, is_admin = s
        label = f"{('ADMIN' if is_admin else tier.upper()):<6} | {chat_id} | {username or '-'}"
        # Caption with timestamp
        caption = f"📊 Daily Scan Report — {datetime.now().strftime('%d %b %Y')}"
        ok, err = send_document(chat_id, scan_file, caption)

        if ok and corp_file:
            time.sleep(0.4)  # rate limit safety
            send_document(chat_id, corp_file,
                          f"📅 Weekly Corp Actions — {datetime.now().strftime('%d %b %Y')}")

        if ok:
            update_send_status(chat_id, "sent")
            sent += 1
            print(f"   ✅ {label}")
        else:
            update_send_status(chat_id, err or "failed")
            failed += 1
            print(f"   ❌ {label}: {err}")
        time.sleep(0.4)  # Telegram-friendly pacing

    print("\n" + "=" * 65)
    print(f"   Sent: {sent} | Failed: {failed}")


if __name__ == "__main__":
    main()
