"""
Diagnose Scanner Performance
─────────────────────────────
Times each stage of the daily flow to find where the 20 minutes go.
Does NOT modify any data. Safe to run anytime.

Usage:
    python diagnose_scanner.py
"""

import time
from datetime import datetime
import yfinance as yf

from db import (
    test_connection,
    get_all_stocks,
    get_latest_trade_date,
    fetch_prices_df,
    fetch_index_df,
    get_cursor,
)

# ─────────────────────────────────────────────
# TIMING DECORATOR
# ─────────────────────────────────────────────
def timed(label):
    def decorator(fn):
        def wrapped(*args, **kwargs):
            t0 = time.time()
            r = fn(*args, **kwargs)
            elapsed = time.time() - t0
            print(f"   ⏱  {label:<45} {elapsed:>6.2f}s")
            return r, elapsed
        return wrapped
    return decorator


# ─────────────────────────────────────────────
# STAGE TESTS
# ─────────────────────────────────────────────
@timed("DB connection test")
def test_db():
    return test_connection()


@timed("List active stocks")
def list_stocks():
    return get_all_stocks(active_only=True)


@timed("Single stock DB read (fetch_prices_df)")
def db_read_single(symbol):
    return fetch_prices_df(symbol)


@timed("Single stock latest_date lookup")
def latest_date_single(symbol):
    return get_latest_trade_date(symbol)


@timed("Single stock yfinance call (period=1mo)")
def yf_single_short(symbol):
    df = yf.download(f"{symbol}.NS", period="1mo", interval="1d",
                     progress=False, auto_adjust=True)
    return df


@timed("Single stock yfinance call (period=5d)")
def yf_single_tiny(symbol):
    df = yf.download(f"{symbol}.NS", period="5d", interval="1d",
                     progress=False, auto_adjust=True)
    return df


def time_full_yf_loop(symbols, max_count=20):
    """Time how long the incremental fetch takes for a sample of stocks."""
    print(f"\n   Testing yfinance call rate on {max_count} sample stocks...")
    sample = symbols[:max_count]
    t0 = time.time()
    no_data = 0
    for sym in sample:
        try:
            df = yf.download(f"{sym}.NS", period="5d", interval="1d",
                             progress=False, auto_adjust=True)
            if df is None or df.empty:
                no_data += 1
        except Exception:
            no_data += 1
    elapsed = time.time() - t0
    per_call = elapsed / max_count
    print(f"   ⏱  {max_count} yfinance calls took {elapsed:.2f}s  ({per_call:.3f}s each)")
    print(f"   📊 Estimated for {len(symbols)} stocks: ~{per_call * len(symbols):.0f}s "
          f"({per_call * len(symbols) / 60:.1f} min)")
    if no_data:
        print(f"   ⚠️  {no_data}/{max_count} returned no data")
    return elapsed


def check_db_freshness(symbols, max_check=20):
    """Check how many stocks need updates vs are already current."""
    today = datetime.now().date()
    sample = symbols[:max_check]
    needs_update = 0
    current = 0
    stale = 0
    for sym in sample:
        latest = get_latest_trade_date(sym)
        if latest is None:
            needs_update += 1
        else:
            days_behind = (today - latest).days
            if days_behind <= 1:
                current += 1
            elif days_behind <= 5:
                needs_update += 1
            else:
                stale += 1
    print(f"\n   Sample of {max_check} stocks:")
    print(f"      ✅ Already up to date: {current}")
    print(f"      🔄 Needs 1-5 day update: {needs_update}")
    print(f"      ⚠️  Stale (>5 days): {stale}")
    return current, needs_update, stale


def check_index_status():
    """Check how many sector indices have data."""
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT index_name, COUNT(*) as rows,
                   MIN(trade_date) as first, MAX(trade_date) as last
            FROM index_prices
            GROUP BY index_name
            ORDER BY index_name
        """)
        rows = cur.fetchall()

    if not rows:
        print("   ⚠️  No index data at all!")
        return

    print(f"\n   Found {len(rows)} indices in DB:")
    for name, count, first, last in rows:
        marker = "✅" if count > 100 else "⚠️ "
        print(f"      {marker} {name:<28} {count:>5} rows | {first} → {last}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    print("="*60)
    print("  SCANNER PERFORMANCE DIAGNOSIS")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print("="*60)

    print("\n[1] Basic timing")
    test_db()
    stocks, _ = list_stocks()
    print(f"      Found {len(stocks)} active stocks")

    print("\n[2] Single-call timings (RELIANCE)")
    db_read_single("RELIANCE")
    latest_date_single("RELIANCE")
    yf_single_short("RELIANCE")
    yf_single_tiny("RELIANCE")

    print("\n[3] DB freshness check")
    check_db_freshness(stocks)

    print("\n[4] yfinance throughput test")
    time_full_yf_loop(stocks, max_count=20)

    print("\n[5] Index data status")
    check_index_status()

    print("\n" + "="*60)
    print("  INTERPRETATION GUIDE")
    print("="*60)
    print("""
  If yfinance per-call is > 2s:
    → That's the main bottleneck. We need request batching or caching.
  If 'Already up to date' is high but scanner still hits yfinance:
    → The scanner's incremental_fetch is too eager. We can skip up-to-date stocks.
  If many indices show as missing/insufficient:
    → Sector synthesis (sector_index.py) will help.
  If DB reads are slow:
    → Need indexes or connection pooling.
""")


if __name__ == "__main__":
    main()
