"""
PATCH SCRIPT — apply bootstrap fixes
─────────────────────────────────────
Addresses these specific issues found after first bootstrap:

  STOCK CHANGES (handled in DB):
    • LTIM    → LTM       (rename, Feb 27, 2026)
    • TIPS    → TIPSMUSIC (rename, Sep 12, 2024) + demerger of TIPSFILMS

  INDEX FIXES (load missing indices via robust fetch with fallback):
    • Nifty Healthcare       — previously failed with "Period 'max' is invalid"
    • Nifty Chemicals        — bad ticker, try alternative
    • Nifty Consumer Durables — fallback needed
    • Nifty Oil & Gas        — fallback needed

  ADDITIONS:
    • Nifty India Defence (new sector index)

Usage:
    python patch_fixes.py            # run everything
    python patch_fixes.py --renames  # only stock renames
    python patch_fixes.py --indices  # only retry missing indices
"""

import sys
import time
import random
from datetime import datetime, timedelta

import yfinance as yf

from db import (
    test_connection,
    get_cursor,
    upsert_stock,
    upsert_sector_mapping,
    delete_prices,
    insert_daily_prices,
    insert_index_prices,
    get_latest_index_date,
    mark_data_refreshed,
    start_job_run,
    finish_job_run,
)


# ─────────────────────────────────────────────
# Robust index fetch — handles yfinance's quirky period='max' rejection
# ─────────────────────────────────────────────
def _robust_fetch(yf_ticker, years_back=20):
    """
    Try period='max' first; if yfinance complains, fall back to start= date.
    Some Nifty sector indices on yfinance ONLY accept short periods,
    so we walk through fallbacks.
    """
    attempts = [
        {"period": "max"},
        {"start": (datetime.now() - timedelta(days=years_back * 365)).strftime("%Y-%m-%d")},
        {"start": (datetime.now() - timedelta(days=10 * 365)).strftime("%Y-%m-%d")},
        {"start": (datetime.now() - timedelta(days=5 * 365)).strftime("%Y-%m-%d")},
        {"period": "5y"},
        {"period": "1y"},
    ]
    last_err = None
    for kwargs in attempts:
        try:
            df = yf.download(yf_ticker, interval="1d",
                             progress=False, auto_adjust=True,
                             **kwargs)
            if df is None or df.empty:
                continue
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            return df, kwargs
        except Exception as e:
            last_err = str(e)
            continue
    return None, last_err


# ─────────────────────────────────────────────
# Symbol-rename helper — logs to symbol_history + moves prices forward
# ─────────────────────────────────────────────
def rename_stock(old_symbol, new_symbol, change_date_str, reason, sectors):
    """
    Mark old symbol inactive (preserve its history under the OLD name
    for audit), add the NEW symbol, and fetch its fresh history.
    """
    print(f"\n🔁 Rename: {old_symbol} → {new_symbol}  ({reason})")

    # Log to symbol_history
    with get_cursor() as (_, cur):
        cur.execute("""
            INSERT INTO symbol_history (old_symbol, new_symbol, change_date, reason)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (old_symbol, change_date) DO NOTHING
        """, (old_symbol, new_symbol, change_date_str, reason))

        # Mark the old stock inactive (keep prices for audit)
        cur.execute("""
            UPDATE stocks SET is_active = FALSE,
                              notes = COALESCE(notes,'') || %s
            WHERE symbol = %s
        """, (f" | Renamed to {new_symbol} on {change_date_str}", old_symbol))

    # Register the new stock and its sectors
    upsert_stock(symbol=new_symbol,
                 yfinance_ticker=f"{new_symbol}.NS",
                 company_name=None,
                 isin=None,
                 is_active=True)
    upsert_sector_mapping(new_symbol, sectors)

    # Fetch fresh history for the new symbol
    df, used = _robust_fetch(f"{new_symbol}.NS")
    if df is None or df.empty:
        print(f"   ⚠️  No data from yfinance for {new_symbol}.NS — verify ticker on Yahoo Finance")
        return False

    rows = insert_daily_prices(new_symbol, df)
    mark_data_refreshed(new_symbol)
    print(f"   ✅ {new_symbol}: {rows} rows | "
          f"{df.index.min().date()} → {df.index.max().date()}  (using {used})")
    return True


def add_new_listing(symbol, sectors, reason):
    """For newly-listed stocks like TIPSFILMS (demerger spin-off)."""
    print(f"\n➕ New listing: {symbol}  ({reason})")
    upsert_stock(symbol=symbol,
                 yfinance_ticker=f"{symbol}.NS",
                 company_name=None,
                 isin=None,
                 is_active=True)
    upsert_sector_mapping(symbol, sectors)
    df, used = _robust_fetch(f"{symbol}.NS")
    if df is None or df.empty:
        print(f"   ⚠️  No data for {symbol}.NS")
        return False
    rows = insert_daily_prices(symbol, df)
    mark_data_refreshed(symbol)
    print(f"   ✅ {symbol}: {rows} rows | "
          f"{df.index.min().date()} → {df.index.max().date()}  (using {used})")
    return True


# ─────────────────────────────────────────────
# 1. STOCK RENAMES & NEW LISTINGS
# ─────────────────────────────────────────────
def apply_stock_changes():
    print("\n" + "="*60)
    print("  APPLYING STOCK CHANGES")
    print("="*60)

    job_id = start_job_run("PATCH_STOCK_CHANGES")
    processed = 0

    # LTIM → LTM  (Feb 27, 2026 rename)
    if rename_stock("LTIM", "LTM",
                    "2026-02-27",
                    "Rename: LTIMindtree Limited → LTM Limited",
                    ["Nifty IT"]):
        processed += 1
    time.sleep(1.0)

    # TIPS → TIPSMUSIC  (Sep 12, 2024 rename of legacy entity)
    if rename_stock("TIPS", "TIPSMUSIC",
                    "2024-09-12",
                    "Rename: Tips Industries → Tips Music",
                    ["Nifty Media"]):
        processed += 1
    time.sleep(1.0)

    # TIPSFILMS (new listing from earlier demerger, Mar 23, 2022)
    if add_new_listing("TIPSFILMS",
                       ["Nifty Media"],
                       "Demerger of Tips Industries film division"):
        processed += 1
    time.sleep(1.0)

    finish_job_run(job_id, "SUCCESS", stocks_processed=processed)
    print(f"\n   ✅ Stock changes applied: {processed}")


# ─────────────────────────────────────────────
# 2. INDEX FIXES (retry the ones that failed + add Defence)
# ─────────────────────────────────────────────
INDEX_RETRY_LIST = {
    # name: (primary_ticker, fallback_tickers)
    "Nifty Healthcare":        ("^CNXHC",            ["NIFTY_HEALTHCARE.NS", "NIFTYHEALTH.NS"]),
    "Nifty Chemicals":         ("^CNXCHEM",          ["NIFTY_CHEM.NS", "NIFTYCHEM.NS"]),
    "Nifty Consumer Durables": ("^CNXCONSDUR",       ["NIFTY_CONSR_DURBL.NS", "NIFTYCONSUMDUR.NS"]),
    "Nifty Oil & Gas":         ("^CNXOILGAS",        ["NIFTY_OIL_AND_GAS.NS", "NIFTYOILGAS.NS"]),
    "Nifty India Defence":     ("NIFTY_IND_DEFENCE.NS", ["NIFTYDEFENCE.NS", "^CNXDEFENCE"]),
}


def _try_index(name, tickers_to_try):
    """Try each ticker until one returns data."""
    for ticker in tickers_to_try:
        df, used = _robust_fetch(ticker)
        if df is not None and not df.empty:
            return df, ticker, used
    return None, None, None


def apply_index_fixes():
    print("\n" + "="*60)
    print("  RETRYING MISSING INDICES")
    print("="*60)

    job_id = start_job_run("PATCH_INDEX_FIXES")
    processed = 0
    failed    = []

    for name, (primary, fallbacks) in INDEX_RETRY_LIST.items():
        latest = get_latest_index_date(name)
        if latest:
            days_old = (datetime.now().date() - latest).days
            if days_old <= 7:
                print(f"\n  {name:<28} ⏭️  already loaded recently")
                continue

        print(f"\n  Retrying {name} ...")
        all_tickers = [primary] + fallbacks
        df, used_ticker, used_kwargs = _try_index(name, all_tickers)

        if df is None or df.empty:
            print(f"   ❌ No data from any ticker tried: {all_tickers}")
            failed.append(name)
            continue

        rows = insert_index_prices(name, df)
        print(f"   ✅ {rows} rows | {df.index.min().date()} → {df.index.max().date()} "
              f"| ticker={used_ticker} kwargs={used_kwargs}")
        processed += 1
        time.sleep(random.uniform(0.5, 1.0))

    if failed:
        print(f"\n   ⚠️  These indices still failed: {', '.join(failed)}")
        print("       They will simply show 'Data Unavailable' in scanner output")
        print("       (scanner handles this gracefully). You can also try")
        print("       computing the sector view from constituent stocks instead.")

    finish_job_run(job_id, "SUCCESS" if not failed else "PARTIAL",
                   stocks_processed=processed,
                   error_message=f"failed_indices={failed}" if failed else None)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    args = set(sys.argv[1:])

    print("="*60)
    print("  PATCH FIXES")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print("="*60)

    try:
        v = test_connection()
        print(f"\n✅ Connected: {v[:60]}...")
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        sys.exit(1)

    if not args or "--renames" in args:
        apply_stock_changes()

    if not args or "--indices" in args:
        apply_index_fixes()

    print("\n✅ Patch complete.")


if __name__ == "__main__":
    main()
