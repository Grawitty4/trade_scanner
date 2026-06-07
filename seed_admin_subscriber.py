"""
Seed an Admin Subscriber
─────────────────────────
Adds you (or any user) to telegram_subscribers as an admin.
Admins receive the daily scan AND error alerts.

Find your chat_id:
  1. Start a chat with your bot on Telegram
  2. Send /start (or any message)
  3. Visit: https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
  4. Look for "from":{"id":...}  — that number is your chat_id

Usage:
    python seed_admin_subscriber.py 123456789 "Your Name"
    python seed_admin_subscriber.py 123456789 "Your Name" --tier admin
"""

import sys
from datetime import datetime
from db import test_connection, get_cursor


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)

    chat_id   = int(args[0])
    full_name = args[1]
    tier      = "admin"
    if "--tier" in sys.argv:
        i = sys.argv.index("--tier")
        if i + 1 < len(sys.argv):
            tier = sys.argv[i + 1]

    is_admin = (tier == "admin")
    if is_admin and tier != "paid":
        # Admins also have paid-equivalent access by convention
        # We keep tier='admin' so we can identify them, but they get full report
        pass

    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        sys.exit(1)

    with get_cursor() as (_, cur):
        cur.execute("""
            INSERT INTO telegram_subscribers
                (chat_id, full_name, tier, is_admin, opted_in)
            VALUES (%s, %s, %s, %s, TRUE)
            ON CONFLICT (chat_id) DO UPDATE SET
                full_name = EXCLUDED.full_name,
                tier      = EXCLUDED.tier,
                is_admin  = EXCLUDED.is_admin,
                opted_in  = TRUE
        """, (chat_id, full_name, tier, is_admin))

    print(f"✅ Subscriber upserted: chat_id={chat_id} name='{full_name}' tier={tier} admin={is_admin}")


if __name__ == "__main__":
    main()
