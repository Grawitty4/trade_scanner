"""
AD-HOC STOCK REFRESH — Run after corporate actions.
Deletes existing history and re-fetches from yfinance with auto-adjust.

Usage:
    python refresh_stock.py VEDL                     # single stock
    python refresh_stock.py VEDL TATASTEEL RELIANCE  # multiple
    python refresh_stock.py --all-flagged            # all flagged PENDING corp actions
    python refresh_stock.py --force VEDL             # skip confirmation
"""

import sys
import time
import yfinance as yf
from datetime import datetime

from db import (
    test_connection,
    delete_prices,
    insert_daily_prices,
    mark_data_refreshed,
    start_job_run,
    finish_job_run,
    get_cursor,
)
from corporate_actions import get_flagged_symbols, mark_decision


def _confirm(prompt):
    try:
        ans = input(prompt + " [y/N]: ").strip().lower()
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _existing_row_count(symbol):
    with get_cursor() as (_, cur):
        cur.execute("SELECT COUNT(*) FROM daily_prices WHERE symbol = %s", (symbol,))
        return cur.fetchone()[0]


def refresh(symbol, force=False):
    """Delete + re-fetch + insert for a single stock."""
    print(f"\n🔁 Refreshing {symbol}...")
    existing = _existing_row_count(symbol)
    print(f"   Existing rows: {existing}")

    if existing > 0 and not force:
        if not _confirm(f"   Delete {existing} rows for {symbol} and re-fetch?"):
            print("   ⏭️  Skipped")
            return False

    # Fetch first; only delete if fetch succeeded
    try:
        df = yf.download(f"{symbol}.NS", period="max", interval="1d",
                         progress=False, auto_adjust=True)
    except Exception as e:
        print(f"   ❌ yfinance fetch failed: {e}")
        return False

    if df is None or df.empty:
        print(f"   ❌ No data returned from yfinance — keeping existing data")
        return False

    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

    deleted = delete_prices(symbol)
    rows    = insert_daily_prices(symbol, df)
    mark_data_refreshed(symbol)

    print(f"   ✅ Deleted {deleted}, inserted {rows} | "
          f"{df.index.min().date()} → {df.index.max().date()}")

    # Update corp action user_decision for any matching PENDING entry
    with get_cursor() as (_, cur):
        cur.execute("""
            UPDATE corporate_actions
            SET user_decision = 'REFRESHED'
            WHERE symbol = %s AND user_decision = 'PENDING'
        """, (symbol,))

    return True


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    force = False
    if "--force" in args:
        force = True
        args.remove("--force")

    print("="*60)
    print("  STOCK REFRESH UTILITY")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print("="*60)

    try:
        v = test_connection()
        print(f"\n✅ Connected: {v[:60]}...")
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        sys.exit(1)

    job_id = start_job_run("REFRESH_STOCK", notes=" ".join(args))

    targets = []
    if "--all-flagged" in args:
        flagged = get_flagged_symbols()
        if not flagged:
            print("\n   ℹ️  No PENDING flagged stocks. Nothing to refresh.")
            finish_job_run(job_id, "SUCCESS", stocks_processed=0)
            return
        targets = list({f["symbol"] for f in flagged})
        print(f"\n   Will refresh {len(targets)} flagged stock(s): "
              f"{', '.join(sorted(targets))}")
        if not force and not _confirm("\nProceed?"):
            finish_job_run(job_id, "CANCELLED", stocks_processed=0)
            return
        force = True   # already confirmed once
    else:
        targets = [a for a in args if not a.startswith("--")]

    if not targets:
        print("\n   ⚠️  No stocks specified.")
        finish_job_run(job_id, "CANCELLED", stocks_processed=0)
        return

    success = 0
    for sym in targets:
        try:
            if refresh(sym, force=force):
                success += 1
        except Exception as e:
            print(f"   ❌ {sym}: {e}")
        time.sleep(1.0)  # be polite to yfinance

    finish_job_run(job_id, "SUCCESS" if success == len(targets) else "PARTIAL",
                   stocks_processed=success)
    print(f"\n✅ Refresh complete: {success}/{len(targets)} succeeded.")


if __name__ == "__main__":
    main()
