"""
Fix LTM History
────────────────
LTM (formerly LTIM) only has 57 rows from Feb 27, 2026 onwards.
yfinance keeps the OLD ticker (LTIM.NS) active and serves the full
historical data there. We pull from the OLD ticker and store under LTM.

This is also a generalizable pattern: for any renamed stock,
fall back to the old yfinance ticker if the new one has insufficient history.

Usage:
    python fix_ltm.py
"""

import time
from datetime import datetime, timedelta

import yfinance as yf
import pandas as pd

from db import (
    test_connection,
    get_cursor,
    delete_prices,
    insert_daily_prices,
    mark_data_refreshed,
    start_job_run,
    finish_job_run,
)


# ─────────────────────────────────────────────
# RENAME MAP: new_symbol -> old yfinance ticker
# Used when yfinance still serves history under the old name
# ─────────────────────────────────────────────
RENAME_FALLBACKS = {
    # symbol_in_db : old_yfinance_ticker
    "LTM": "LTIM.NS",
    # Add more here as you discover them:
    # "TMPV": "TATAMOTORS.NS",     # if TMPV ever has insufficient history
}


def _robust_fetch(yf_ticker):
    """Cascade through period/start options."""
    attempts = [
        {"period": "max"},
        {"start": (datetime.now() - timedelta(days=20 * 365)).strftime("%Y-%m-%d")},
        {"start": (datetime.now() - timedelta(days=10 * 365)).strftime("%Y-%m-%d")},
        {"period": "5y"},
    ]
    for kwargs in attempts:
        try:
            df = yf.download(yf_ticker, interval="1d",
                             progress=False, auto_adjust=True, **kwargs)
            if df is None or df.empty:
                continue
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            return df, kwargs
        except Exception:
            continue
    return None, None


def _row_count(symbol):
    with get_cursor() as (_, cur):
        cur.execute("SELECT COUNT(*) FROM daily_prices WHERE symbol = %s", (symbol,))
        return cur.fetchone()[0]


def fix_renamed_stock(new_symbol, old_yf_ticker, min_rows=200):
    """
    If a stock has fewer than min_rows of history, try fetching from the
    old yfinance ticker (which often retains the long history).
    """
    print(f"\n🔧 Checking {new_symbol} (fallback ticker: {old_yf_ticker})")
    existing = _row_count(new_symbol)
    print(f"   Current row count: {existing}")

    if existing >= min_rows:
        print(f"   ✅ Already has sufficient history (>{min_rows} rows). Skipping.")
        return False

    print(f"   ⚠️  Insufficient history. Fetching from {old_yf_ticker}...")
    df, used = _robust_fetch(old_yf_ticker)
    if df is None or df.empty:
        print(f"   ❌ {old_yf_ticker} also returned no data")
        return False

    # Also fetch from the new ticker to catch the most recent days
    new_df, new_used = _robust_fetch(f"{new_symbol}.NS")
    if new_df is not None and not new_df.empty:
        # Combine — new ticker takes precedence for overlapping dates
        df = pd.concat([df, new_df])
        df = df[~df.index.duplicated(keep='last')].sort_index()
        print(f"   ✅ Merged history from both tickers")

    # Replace the partial data
    deleted = delete_prices(new_symbol)
    rows    = insert_daily_prices(new_symbol, df)
    mark_data_refreshed(new_symbol)

    print(f"   ✅ Deleted {deleted}, inserted {rows} | "
          f"{df.index.min().date()} → {df.index.max().date()}")

    # Update the yfinance_ticker field so future runs know to use the old name
    with get_cursor() as (_, cur):
        cur.execute("""
            UPDATE stocks
            SET yfinance_ticker = %s,
                notes = COALESCE(notes,'') || %s
            WHERE symbol = %s
        """, (old_yf_ticker,
              f" | History fetched from {old_yf_ticker} on {datetime.now().date()}",
              new_symbol))

    return True


def main():
    print("="*60)
    print("  FIX RENAMED STOCK HISTORY")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print("="*60)

    try:
        v = test_connection()
        print(f"\n✅ Connected: {v[:60]}...")
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return

    job_id = start_job_run("FIX_RENAMED_STOCKS")
    fixed = 0
    for new_sym, old_yf in RENAME_FALLBACKS.items():
        if fix_renamed_stock(new_sym, old_yf):
            fixed += 1
        time.sleep(1.5)

    finish_job_run(job_id, "SUCCESS", stocks_processed=fixed)
    print(f"\n✅ Fixed {fixed} stocks.")


if __name__ == "__main__":
    main()
