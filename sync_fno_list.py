"""
Sync F&O Universe from NSE
───────────────────────────
Downloads NSE's authoritative F&O market lots CSV and updates `is_fno` flag.
  • Symbols on the NSE F&O list → is_fno = True
  • Symbols NOT on the list but currently is_fno=True → auto-demoted to is_fno=False
  • New F&O symbols not in our DB → inserted (with is_fno=True, is_active=True)

NSE updates this list periodically (additions/removals based on liquidity).
SOLARINDS-type additions get caught automatically.

Usage:
    python sync_fno_list.py            # dry-run preview
    python sync_fno_list.py --commit   # apply changes
"""

import sys
import io
import csv
import re
import requests
from datetime import datetime

from db import test_connection, get_cursor, upsert_stock, start_job_run, finish_job_run

NSE_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/csv,application/csv,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_fno_csv():
    """Download F&O market lots CSV from NSE."""
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception:
        pass
    try:
        resp = session.get(NSE_URL, timeout=30)
        if resp.status_code != 200:
            print(f"   ❌ NSE returned HTTP {resp.status_code}")
            return None
        return resp.text
    except Exception as e:
        print(f"   ❌ Fetch failed: {e}")
        return None


def parse_fno_csv(csv_text):
    """
    NSE's fo_mktlots.csv has a quirky format — multi-row header, blank lines.
    Extract just the SYMBOL column. Symbols are uppercase A-Z plus digits/hyphens.
    """
    symbols = set()
    # Each non-empty row's first column that looks like a symbol
    reader = csv.reader(io.StringIO(csv_text))
    for row in reader:
        if not row or len(row) < 2:
            continue
        candidate = row[1].strip() if len(row) > 1 else ""
        # Real NSE symbols are uppercase letters, digits, and limited punctuation
        if candidate and re.match(r"^[A-Z][A-Z0-9&\-]{1,15}$", candidate):
            # Filter out known header tokens
            if candidate in ("SYMBOL", "UNDERLYING", "INDEX"):
                continue
            symbols.add(candidate)
    return symbols


def get_current_fno_state():
    """Returns set of symbols currently marked is_fno=True."""
    with get_cursor() as (_, cur):
        cur.execute("SELECT symbol FROM stocks WHERE is_fno = TRUE AND is_active = TRUE")
        return {r[0] for r in cur.fetchall()}


def get_all_active_symbols():
    """Returns set of all active symbols."""
    with get_cursor() as (_, cur):
        cur.execute("SELECT symbol FROM stocks WHERE is_active = TRUE")
        return {r[0] for r in cur.fetchall()}


def main():
    commit = "--commit" in sys.argv

    print("=" * 65)
    print("  SYNC F&O UNIVERSE FROM NSE")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print(f"  Mode: {'COMMIT' if commit else 'DRY-RUN'}")
    print("=" * 65)

    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return

    print("\n[1/3] Downloading NSE F&O market lots CSV...")
    csv_text = fetch_fno_csv()
    if not csv_text:
        print("   ❌ Failed to download. Check NSE accessibility.")
        return

    print("\n[2/3] Parsing symbols...")
    nse_fno_symbols = parse_fno_csv(csv_text)
    if not nse_fno_symbols:
        print("   ❌ No symbols parsed — CSV format may have changed.")
        print(f"   First 200 chars: {csv_text[:200]}")
        return
    print(f"   ✅ Parsed {len(nse_fno_symbols)} F&O symbols from NSE")

    print("\n[3/3] Computing diff against DB...")
    current_fno   = get_current_fno_state()
    all_active    = get_all_active_symbols()

    new_to_fno      = nse_fno_symbols - current_fno
    leaving_fno     = current_fno - nse_fno_symbols
    completely_new  = nse_fno_symbols - all_active   # not even in DB yet
    promote_inv     = (new_to_fno - completely_new)  # already in DB as non-F&O

    print(f"\n   Current state:")
    print(f"      F&O stocks in DB        : {len(current_fno)}")
    print(f"      Active stocks in DB     : {len(all_active)}")
    print(f"      F&O symbols on NSE list : {len(nse_fno_symbols)}")
    print(f"\n   Changes proposed:")
    print(f"      ➕ New stocks to add (not in DB) : {len(completely_new)}")
    print(f"      ⬆️  Promote investment → F&O      : {len(promote_inv)}")
    print(f"      ⬇️  Demote F&O → investment       : {len(leaving_fno)}")

    if completely_new:
        sample = sorted(completely_new)[:15]
        print(f"\n   Brand new symbols (sample): {', '.join(sample)}")
        if len(completely_new) > 15:
            print(f"      ... and {len(completely_new) - 15} more")

    if promote_inv:
        sample = sorted(promote_inv)[:15]
        print(f"\n   Promoting to F&O (sample): {', '.join(sample)}")
        if len(promote_inv) > 15:
            print(f"      ... and {len(promote_inv) - 15} more")

    if leaving_fno:
        sample = sorted(leaving_fno)[:15]
        print(f"\n   Demoting from F&O (sample): {', '.join(sample)}")
        if len(leaving_fno) > 15:
            print(f"      ... and {len(leaving_fno) - 15} more")

    if not commit:
        print(f"\n   (dry-run — no DB writes)")
        print(f"   Re-run with --commit to apply.")
        if completely_new:
            print(f"\n   ⚠️  {len(completely_new)} brand new symbols will need bootstrap data after commit.")
            print(f"      Run: python bootstrap_new_stocks.py --commit")
        return

    # Commit
    job_id = start_job_run("SYNC_FNO_LIST")
    added, promoted, demoted, failed = 0, 0, 0, 0

    for symbol in completely_new:
        try:
            upsert_stock(symbol=symbol,
                         yfinance_ticker=f"{symbol}.NS",
                         is_fno=True, is_active=True)
            added += 1
        except Exception as e:
            failed += 1
            print(f"   ❌ {symbol}: {e}")

    for symbol in promote_inv:
        try:
            with get_cursor() as (_, cur):
                cur.execute("""
                    UPDATE stocks SET is_fno = TRUE, updated_at = NOW()
                    WHERE symbol = %s
                """, (symbol,))
            promoted += 1
        except Exception:
            failed += 1

    for symbol in leaving_fno:
        try:
            with get_cursor() as (_, cur):
                cur.execute("""
                    UPDATE stocks SET is_fno = FALSE, updated_at = NOW()
                    WHERE symbol = %s
                """, (symbol,))
            demoted += 1
        except Exception:
            failed += 1

    finish_job_run(job_id, "SUCCESS" if failed == 0 else "PARTIAL",
                   stocks_processed=added + promoted + demoted,
                   error_message=f"failed={failed}" if failed else None)

    print("\n" + "=" * 65)
    print("  SUMMARY")
    print("=" * 65)
    print(f"   ➕ Added new F&O symbols : {added}")
    print(f"   ⬆️  Promoted to F&O       : {promoted}")
    print(f"   ⬇️  Demoted from F&O      : {demoted}")
    if failed:
        print(f"   ❌ Failed                : {failed}")
    if added:
        print(f"\n   Next step: python bootstrap_new_stocks.py --commit")
        print(f"   (Pulls historical OHLCV for the {added} brand new symbols)")


if __name__ == "__main__":
    main()
