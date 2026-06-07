"""
Daily Pipeline Orchestrator
────────────────────────────
The single entry point that Railway cron triggers.

Executes (in order):
  1. update_corporate_actions()       (NSE corp action fetcher)
  2. market_scanner.run_full_scan()   (the daily scan)
  3. telegram_dispatch                (send report to subscribers)

Behavior:
  • Logs every step's success/failure into job_runs table
  • On ANY failure, sends a Telegram alert to admin chat_ids
  • The 5 AM IST recovery cron checks job_runs to decide whether to re-trigger
  • Exits with code 0 on full success, 1 on any failure

Usage:
    python run_daily.py            # full pipeline
    python run_daily.py --skip-dispatch  # don't send Telegram (for local testing)
"""

import sys
import traceback
from datetime import datetime, date
from db import test_connection, get_cursor, start_job_run, finish_job_run


# ─────────────────────────────────────────────
# CHECK IF ALREADY RAN SUCCESSFULLY TODAY
# (used by the 5 AM recovery to avoid double-runs)
# ─────────────────────────────────────────────
def already_succeeded_today():
    """Returns True if a DAILY_PIPELINE row with status=SUCCESS exists for today."""
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT 1 FROM job_runs
            WHERE run_date = CURRENT_DATE
              AND job_type = 'DAILY_PIPELINE'
              AND status   = 'SUCCESS'
            LIMIT 1
        """)
        return cur.fetchone() is not None


# ─────────────────────────────────────────────
# STAGE RUNNERS
# ─────────────────────────────────────────────
def run_scanner():
    """Imports and runs the scanner. Returns (success, report_path_or_error)."""
    try:
        # Import here so module-level errors are caught
        from market_scanner import run_full_scan, format_results, format_corp_action_report
        results = run_full_scan()
        report  = format_results(results)

        # Write daily file
        daily_name = f"scan_result_{datetime.now().strftime('%d_%b_%Y')}.txt"
        with open(daily_name, "w", encoding="utf-8") as f:
            f.write(report)

        # Monday-only corp action file
        if datetime.now().weekday() == 0:
            corp_name = f"corp_actions_{datetime.now().strftime('%d_%b_%Y')}.txt"
            with open(corp_name, "w", encoding="utf-8") as f:
                f.write(format_corp_action_report(results))

        return True, daily_name
    except Exception as e:
        return False, f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[:2000]}"


def run_dispatch(file_path):
    """Sends the report via Telegram. Returns (success, error_or_None)."""
    try:
        # Override sys.argv so dispatch picks up the right file
        old_argv = sys.argv
        sys.argv = ["telegram_dispatch.py", "--file", file_path]
        try:
            from telegram_dispatch import main as dispatch_main
            dispatch_main()
        finally:
            sys.argv = old_argv
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[:2000]}"


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    skip_dispatch = "--skip-dispatch" in sys.argv
    is_recovery   = "--recovery" in sys.argv

    print("=" * 65)
    print("  DAILY PIPELINE")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    if is_recovery:
        print(f"  Mode: RECOVERY RUN (5 AM IST safety net)")
    print("=" * 65)

    # If this is the recovery run and we already succeeded earlier today, exit cleanly
    if is_recovery:
        try:
            if already_succeeded_today():
                print("\n✅ Pipeline already completed successfully today. Nothing to do.")
                sys.exit(0)
            else:
                print("\n⚠️  No successful run today — proceeding with recovery.")
        except Exception as e:
            print(f"⚠️  Couldn't check today's status: {e}; proceeding anyway")

    # Sanity check DB
    try:
        test_connection()
    except Exception as e:
        # Even DB is down — try to alert admins via env-only path
        msg = f"❌ DB unreachable from run_daily: {e}"
        print(msg)
        try:
            from telegram_dispatch import send_error_alert_to_admins
            send_error_alert_to_admins(msg)
        except Exception:
            pass
        sys.exit(1)

    # Start tracking
    job_id = start_job_run("DAILY_PIPELINE", notes="recovery" if is_recovery else "scheduled")

    # Stage 1: scanner
    print("\n[1/2] Running scanner...")
    ok, result = run_scanner()
    if not ok:
        err_msg = f"Scanner failed:\n{result}"
        print(f"❌ {err_msg}")
        finish_job_run(job_id, "FAILED", error_message=err_msg[:1000])
        try:
            from telegram_dispatch import send_error_alert_to_admins
            send_error_alert_to_admins(err_msg)
        except Exception as e:
            print(f"⚠️  Could not send admin alert: {e}")
        sys.exit(1)

    report_file = result
    print(f"✅ Scanner complete — {report_file}")

    # Stage 2: dispatch (skippable for local testing)
    if skip_dispatch:
        print("\n[2/2] Dispatch skipped (--skip-dispatch flag)")
        finish_job_run(job_id, "SUCCESS",
                       notes="dispatch_skipped",
                       stocks_processed=1)
        print("\n✅ Pipeline complete (dispatch skipped).")
        sys.exit(0)

    print("\n[2/2] Sending Telegram dispatch...")
    ok, err = run_dispatch(report_file)
    if not ok:
        err_msg = f"Dispatch failed (scanner DID complete):\n{err}"
        print(f"❌ {err_msg}")
        # Pipeline considered PARTIAL — scanner ran, dispatch didn't
        finish_job_run(job_id, "PARTIAL", error_message=err_msg[:1000])
        try:
            from telegram_dispatch import send_error_alert_to_admins
            send_error_alert_to_admins(err_msg)
        except Exception:
            pass
        sys.exit(1)

    finish_job_run(job_id, "SUCCESS", stocks_processed=1)
    print("\n✅ Pipeline complete — scanner + dispatch SUCCESS.")
    sys.exit(0)


if __name__ == "__main__":
    main()
