"""
Bootstrap New Stocks (Parallelized)
─────────────────────────────────────
Fetches max history from yfinance for stocks that don't have any data
in daily_prices yet. Uses ThreadPoolExecutor for ~5x speedup over serial.

Idempotent: re-running skips stocks already loaded.

Usage:
    python bootstrap_new_stocks.py             # dry-run preview
    python bootstrap_new_stocks.py --commit    # actually fetch + insert
    python bootstrap_new_stocks.py --commit --workers 4   # tune concurrency
"""

import sys
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf

from db import (
    test_connection,
    get_cursor,
    insert_daily_prices,
    mark_data_refreshed,
    start_job_run,
    finish_job_run,
)


def get_unloaded_stocks():
    """Return list of symbols in `stocks` with no rows in `daily_prices`."""
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT s.symbol, s.yfinance_ticker
            FROM stocks s
            LEFT JOIN daily_prices dp ON dp.symbol = s.symbol
            WHERE s.is_active = TRUE
            GROUP BY s.symbol, s.yfinance_ticker
            HAVING COUNT(dp.trade_date) = 0
            ORDER BY s.symbol
        """)
        return cur.fetchall()


def fetch_history(yf_ticker):
    """Robust fetch with cascade. Returns DataFrame or None."""
    attempts = [
        {"period": "max"},
        {"start": (datetime.now() - timedelta(days=20 * 365)).strftime("%Y-%m-%d")},
        {"start": (datetime.now() - timedelta(days=10 * 365)).strftime("%Y-%m-%d")},
        {"period": "5y"},
        {"period": "2y"},
    ]
    for kwargs in attempts:
        try:
            df = yf.download(yf_ticker, interval="1d",
                             progress=False, auto_adjust=True, **kwargs)
            if df is None or df.empty:
                continue
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            return df
        except Exception:
            continue
    return None


def bootstrap_one(symbol, yf_ticker):
    """Worker function. Returns (symbol, rows_inserted, error_or_None)."""
    if not yf_ticker:
        yf_ticker = f"{symbol}.NS"
    try:
        df = fetch_history(yf_ticker)
        if df is None or df.empty:
            return symbol, 0, "no data from yfinance"
        rows = insert_daily_prices(symbol, df)
        if rows > 0:
            mark_data_refreshed(symbol)
        return symbol, rows, None
    except Exception as e:
        return symbol, 0, str(e)


def main():
    args = sys.argv[1:]
    commit = "--commit" in args
    workers = 4  # conservative default for yfinance
    if "--workers" in args:
        i = args.index("--workers")
        if i + 1 < len(args):
            try:
                workers = int(args[i + 1])
            except Exception:
                pass

    print("=" * 65)
    print("  BOOTSTRAP NEW STOCKS (parallelized)")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print(f"  Mode: {'COMMIT' if commit else 'DRY-RUN'}  |  Workers: {workers}")
    print("=" * 65)

    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return

    print("\n[1/2] Finding stocks without historical data...")
    pending = get_unloaded_stocks()
    print(f"   Found {len(pending)} stocks needing bootstrap.")

    if not pending:
        print("\n   ✅ All stocks already have data. Nothing to do.")
        return

    if not commit:
        print(f"\n   Preview of first 10:")
        for sym, yf_tick in pending[:10]:
            print(f"      {sym:<14} → {yf_tick}")
        if len(pending) > 10:
            print(f"      ... and {len(pending) - 10} more")
        print(f"\n   (dry-run — no fetching)")
        print(f"   Re-run with --commit to bootstrap.")
        return

    print(f"\n[2/2] Bootstrapping {len(pending)} stocks ({workers} workers)...")
    job_id = start_job_run("BOOTSTRAP_NEW_STOCKS", notes=f"{len(pending)} stocks")

    start_time = time.time()
    success = 0
    failed = 0
    no_data = 0
    failures = []
    total_rows = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(bootstrap_one, sym, yf_tick): sym
                   for sym, yf_tick in pending}
        completed = 0
        for f in as_completed(futures):
            completed += 1
            try:
                symbol, rows, err = f.result()
                if err:
                    if "no data" in err.lower():
                        no_data += 1
                        failures.append((symbol, "no yfinance data"))
                    else:
                        failed += 1
                        failures.append((symbol, err[:80]))
                else:
                    success += 1
                    total_rows += rows
                if completed % 20 == 0 or completed == len(pending):
                    elapsed = time.time() - start_time
                    rate = completed / elapsed if elapsed > 0 else 0
                    remaining = (len(pending) - completed) / rate if rate > 0 else 0
                    print(f"   [{completed}/{len(pending)}] "
                          f"success={success} no_data={no_data} failed={failed}  "
                          f"({rate:.1f}/sec, ETA {remaining:.0f}s)")
            except Exception as e:
                failed += 1
                failures.append((futures[f], str(e)[:80]))

    elapsed = time.time() - start_time
    finish_job_run(
        job_id,
        "SUCCESS" if (failed == 0 and no_data == 0) else "PARTIAL",
        stocks_processed=success,
        error_message=f"failed={failed} no_data={no_data}" if (failed or no_data) else None,
    )

    print("\n" + "=" * 65)
    print("  SUMMARY")
    print("=" * 65)
    print(f"   Total stocks attempted:  {len(pending)}")
    print(f"   ✅ Successful:           {success}  ({total_rows:,} total rows)")
    print(f"   ⚠️  No yfinance data:    {no_data}")
    print(f"   ❌ Failed:               {failed}")
    print(f"   ⏱  Time:                {elapsed:.1f}s ({len(pending)/elapsed:.1f}/sec avg)")

    if failures:
        print(f"\n   First 20 failures:")
        for sym, reason in failures[:20]:
            print(f"      {sym:<14}  {reason}")
        if len(failures) > 20:
            print(f"      ... and {len(failures) - 20} more")
        print(f"\n   Failed stocks remain in `stocks` table but have no price data.")
        print(f"   They will be silently skipped by the scanner (needs >=60 rows).")
        print(f"   You can re-run this script anytime to retry them.")

    print(f"\n   Next: run scanner — it will now include NIFTY 500 stocks")


if __name__ == "__main__":
    main()
