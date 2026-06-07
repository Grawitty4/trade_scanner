"""
Clean Re-pull
─────────────
For each specified stock:
  1. Delete its rows from corporate_action_adjustments (any state)
  2. Delete its rows from daily_prices
  3. Re-fetch max history from yfinance (auto_adjust=True)
  4. Insert fresh
  5. Verify: report price gaps around known corp action dates

Usage:
    python clean_repull.py VEDL HINDUNILVR BAJFINANCE
    python clean_repull.py VEDL --commit
    python clean_repull.py VEDL HINDUNILVR BAJFINANCE --commit
"""

import sys
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


# Known corp action dates to verify post-repull (informational only)
KNOWN_EVENTS = {
    "VEDL":       ("2026-04-30", "Demerger into 5 entities"),
    "HINDUNILVR": ("2025-12-05", "Kwality Wall's demerger"),
    "BAJFINANCE": ("2025-06-16", "Split 2:1 + Bonus 4:1"),
}


def _fetch_max_history(yf_ticker):
    """Robust fetch — same cascade we use in bootstrap."""
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


def _check_for_gap(symbol, event_date_str, df):
    """
    Look at the price ratio before/after the known event date.
    If yfinance back-adjusted correctly, the ratio should be near 1.0 (smooth).
    If unadjusted, there'll be a sharp drop.
    """
    try:
        event_dt = datetime.strptime(event_date_str, "%Y-%m-%d").date()
    except Exception:
        return None

    df_sorted = df.sort_index()
    before = df_sorted[df_sorted.index.date < event_dt].tail(1)
    after  = df_sorted[df_sorted.index.date >= event_dt].head(1)

    if before.empty or after.empty:
        return None

    bc = float(before['Close'].iloc[0])
    ac = float(after['Close'].iloc[0])
    if bc == 0:
        return None

    ratio = ac / bc
    return {
        "before_date":  before.index[0].date(),
        "before_close": bc,
        "after_date":   after.index[0].date(),
        "after_close":  ac,
        "ratio":        ratio,
    }


def repull(symbol, commit=False):
    print(f"\n{'─'*65}")
    print(f"  {symbol}")
    print(f"{'─'*65}")

    # Count existing data
    with get_cursor() as (_, cur):
        cur.execute("SELECT COUNT(*) FROM daily_prices WHERE symbol = %s", (symbol,))
        existing_prices = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM corporate_action_adjustments WHERE symbol = %s",
                    (symbol,))
        existing_adj = cur.fetchone()[0]
    print(f"   Existing: {existing_prices} price rows | {existing_adj} adjustment row(s)")

    if not commit:
        print(f"   (dry-run — no deletion or fetch)")
        return False

    # 1. Delete adjustment rows
    with get_cursor() as (_, cur):
        cur.execute("DELETE FROM corporate_action_adjustments WHERE symbol = %s",
                    (symbol,))
        adj_del = cur.rowcount
    print(f"   🗑  Deleted {adj_del} adjustment row(s)")

    # 2. Fetch fresh
    print(f"   📡 Fetching from yfinance ({symbol}.NS)...")
    df, used_kwargs = _fetch_max_history(f"{symbol}.NS")
    if df is None or df.empty:
        print(f"   ❌ Fetch returned no data — keeping existing data unchanged.")
        return False
    print(f"   ✅ Got {len(df)} rows from yfinance | "
          f"{df.index.min().date()} → {df.index.max().date()} | params={used_kwargs}")

    # 3. Delete old prices
    deleted = delete_prices(symbol)
    print(f"   🗑  Deleted {deleted} old price row(s)")

    # 4. Insert fresh
    inserted = insert_daily_prices(symbol, df)
    mark_data_refreshed(symbol)
    print(f"   📥 Inserted {inserted} fresh price row(s)")

    # 5. Verification — check for unaddressed corp action gaps
    if symbol in KNOWN_EVENTS:
        event_date, description = KNOWN_EVENTS[symbol]
        print(f"\n   🔍 Verifying {description}  (event date: {event_date})")
        gap = _check_for_gap(symbol, event_date, df)
        if gap:
            print(f"      Last close before : ₹{gap['before_close']:.2f}  ({gap['before_date']})")
            print(f"      First close after : ₹{gap['after_close']:.2f}  ({gap['after_date']})")
            print(f"      Ratio (after/before): {gap['ratio']:.4f}")

            if 0.85 <= gap['ratio'] <= 1.15:
                print(f"      ✅ Smooth — yfinance has back-adjusted correctly")
            elif gap['ratio'] < 0.85:
                pct_drop = (1 - gap['ratio']) * 100
                print(f"      ⚠️  Apparent {pct_drop:.1f}% drop — yfinance has NOT adjusted for this event")
                print(f"      → You'll need a manual adjustment row with factor ≈ {gap['ratio']:.4f}")
            else:
                pct_jump = (gap['ratio'] - 1) * 100
                print(f"      ⚠️  Apparent {pct_jump:.1f}% jump — investigate (unusual)")
        else:
            print(f"      ⏭️  Could not get before/after pair (data may not span event date)")

    return True


def main():
    args = sys.argv[1:]
    commit = "--commit" in args
    symbols = [a.upper() for a in args if not a.startswith("--")]
    if not symbols:
        print(__doc__)
        return

    print("="*65)
    print("  CLEAN RE-PULL")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print(f"  Mode: {'COMMIT' if commit else 'DRY-RUN'}")
    print(f"  Symbols: {', '.join(symbols)}")
    print("="*65)

    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return

    job_id = start_job_run("CLEAN_REPULL", notes=", ".join(symbols)) if commit else None

    success = 0
    failed  = 0
    for sym in symbols:
        try:
            if repull(sym, commit=commit):
                success += 1
            else:
                failed += 1
        except Exception as e:
            print(f"   ❌ {sym} failed: {e}")
            failed += 1
        time.sleep(1.0)  # polite to yfinance

    if commit:
        finish_job_run(job_id,
                       "SUCCESS" if failed == 0 else "PARTIAL",
                       stocks_processed=success,
                       error_message=f"failed={failed}" if failed else None)
        print(f"\n✅ Re-pulled: {success} | Failed: {failed}")
        print(f"\n   Next: python check_stock.py VEDL  # verify RSI is sensible")


if __name__ == "__main__":
    main()
