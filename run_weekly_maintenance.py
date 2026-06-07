"""
Weekly Maintenance
───────────────────
Sunday-only background tasks:
  1. Sync NSE F&O list (catches new additions like SOLARINDS)
  2. Backfill last 30 days of corp actions (in case daily fetcher missed any)

This runs separately from the daily pipeline so a failure here doesn't
block the daily scanner.

Usage:
    python run_weekly_maintenance.py
"""

import sys
import traceback
from datetime import datetime
from db import test_connection, start_job_run, finish_job_run


def run_fno_sync():
    try:
        import sync_fno_list
        old_argv = sys.argv
        sys.argv = ["sync_fno_list.py", "--commit"]
        try:
            sync_fno_list.main()
        finally:
            sys.argv = old_argv
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[:1500]}"


def run_corp_action_refresh():
    """Re-runs the 30-day NSE fetch (in case the daily scanner missed any)."""
    try:
        from corporate_actions import update_corporate_actions
        update_corporate_actions()
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[:1500]}"


def main():
    print("=" * 65)
    print("  WEEKLY MAINTENANCE")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print("=" * 65)

    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        try:
            from telegram_dispatch import send_error_alert_to_admins
            send_error_alert_to_admins(f"Weekly maintenance: DB unreachable\n{e}")
        except Exception:
            pass
        sys.exit(1)

    job_id = start_job_run("WEEKLY_MAINTENANCE")
    errors = []

    print("\n[1/2] Syncing F&O list from NSE...")
    ok, err = run_fno_sync()
    if not ok:
        print(f"❌ F&O sync failed: {err}")
        errors.append(("FNO_SYNC", err))
    else:
        print("✅ F&O sync complete")

    print("\n[2/2] Refreshing corp actions (30-day window)...")
    ok, err = run_corp_action_refresh()
    if not ok:
        print(f"❌ Corp actions refresh failed: {err}")
        errors.append(("CORP_ACTIONS", err))
    else:
        print("✅ Corp actions refresh complete")

    if errors:
        finish_job_run(job_id, "PARTIAL",
                       error_message=" | ".join(f"{n}: {e[:200]}" for n, e in errors))
        try:
            from telegram_dispatch import send_error_alert_to_admins
            send_error_alert_to_admins(
                "Weekly maintenance had failures:\n\n" +
                "\n\n".join(f"{n}:\n{e}" for n, e in errors)
            )
        except Exception:
            pass
        sys.exit(1)

    finish_job_run(job_id, "SUCCESS")
    print("\n✅ Weekly maintenance complete.")
    sys.exit(0)


if __name__ == "__main__":
    main()
