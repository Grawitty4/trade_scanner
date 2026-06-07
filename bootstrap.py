"""
ONE-TIME BOOTSTRAP — Load max history for all F&O stocks + indices.
Idempotent: re-running picks up where it left off (only fetches missing data).

Usage:
    python bootstrap.py              # full bootstrap
    python bootstrap.py --indices    # only indices
    python bootstrap.py --stocks     # only stocks
    python bootstrap.py --migrate-ca # migrate corporate_actions.json
"""

import sys
import time
import random
import yfinance as yf
from datetime import datetime

from db import (
    init_schema,
    test_connection,
    upsert_stock,
    upsert_sector_mapping,
    insert_daily_prices,
    insert_index_prices,
    get_latest_trade_date,
    get_latest_index_date,
    mark_data_refreshed,
    start_job_run,
    finish_job_run,
    quick_stats,
)
from corporate_actions import migrate_from_json

# ─────────────────────────────────────────────
# STOCK + SECTOR DEFINITIONS
# (Same lists used by the scanner. Single source for now.
#  When we move to Phase B+, these can come from DB only.)
# ─────────────────────────────────────────────
NSE_SECTOR_INDICES = {
    "Nifty Bank":               "^NSEBANK",
    "Nifty Private Bank":       "NIFTY_PVT_BANK.NS",
    "Nifty PSU Bank":           "^CNXPSUBANK",
    "Nifty Financial Services": "NIFTY_FIN_SERVICE.NS",
    "Nifty IT":                 "^CNXIT",
    "Nifty Pharma":             "^CNXPHARMA",
    "Nifty Healthcare":         "^CNXHC",
    "Nifty Auto":               "^CNXAUTO",
    "Nifty FMCG":               "^CNXFMCG",
    "Nifty Metal":              "^CNXMETAL",
    "Nifty Realty":             "^CNXREALTY",
    "Nifty Media":              "^CNXMEDIA",
    "Nifty Chemicals":          "^CNXCHEM",
    "Nifty Consumer Durables":  "^CNXCONSDUR",
    "Nifty Energy":             "^CNXENERGY",
    "Nifty Infra":              "^CNXINFRA",
    "Nifty Oil & Gas":          "^CNXOILGAS",
    "Nifty India Defence":      "NIFTY_IND_DEFENCE.NS",
    "NIFTY 50":                 "^NSEI",
    "SENSEX":                   "^BSESN",
}

SECTOR_STOCKS = {
    "Nifty Bank": [
        "HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK", "SBIN",
        "INDUSINDBK", "BANDHANBNK", "FEDERALBNK", "IDFCFIRSTB",
        "PNB", "BANKBARODA", "AUBANK"
    ],
    "Nifty Private Bank": [
        "HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK", "INDUSINDBK",
        "IDFCFIRSTB", "FEDERALBNK", "BANDHANBNK", "AUBANK", "RBLBANK"
    ],
    "Nifty PSU Bank": [
        "SBIN", "BANKBARODA", "PNB", "CANBK", "UNIONBANK",
        "INDIANB", "BANKINDIA", "IOB", "MAHABANK", "CENTRALBK"
    ],
    "Nifty Financial Services": [
        "HDFCBANK", "ICICIBANK", "SBIN", "BAJFINANCE", "AXISBANK",
        "KOTAKBANK", "BAJAJFINSV", "SBILIFE", "HDFCLIFE", "SHRIRAMFIN",
        "JIOFIN", "PFC", "MUTHOOTFIN", "ICICIGI", "ICICIPRULI",
        "CHOLAFIN", "RECLTD", "LICI", "M&MFIN", "SBICARD"
    ],
    "Nifty IT": [
        "TCS", "INFY", "WIPRO", "HCLTECH", "TECHM",
        "LTM", "MPHASIS", "PERSISTENT", "COFORGE", "OFSS"
    ],
    "Nifty Pharma": [
        "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "BIOCON",
        "AUROPHARMA", "ALKEM", "TORNTPHARM", "LUPIN", "IPCALAB",
        "ZYDUSLIFE", "GLENMARK", "LAURUSLABS", "GRANULES"
    ],
    "Nifty Healthcare": [
        "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "APOLLOHOSP",
        "MAXHEALTH", "FORTIS", "BIOCON", "ALKEM", "TORNTPHARM",
        "LUPIN", "LAURUSLABS", "ZYDUSLIFE", "SYNGENE"
    ],
    "Nifty Auto": [
        "MARUTI", "TMPV", "M&M", "BAJAJ-AUTO", "HEROMOTOCO",
        "EICHERMOT", "ASHOKLEY", "TVSMOTOR", "MOTHERSON", "BALKRISIND",
        "BHARATFORG", "BOSCHLTD", "EXIDEIND"
    ],
    "Nifty FMCG": [
        "HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "DABUR",
        "MARICO", "GODREJCP", "COLPAL", "TATACONSUM", "UBL", "VBL"
    ],
    "Nifty Metal": [
        "TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "SAIL",
        "NMDC", "COALINDIA", "NATIONALUM", "APLAPOLLO", "JINDALSTEL",
        "HINDCOPPER", "RATNAMANI"
    ],
    "Nifty Realty": [
        "DLF", "GODREJPROP", "OBEROIRLTY", "PRESTIGE", "PHOENIXLTD",
        "BRIGADE", "SOBHA", "MAHLIFE", "LODHA"
    ],
    "Nifty Media": [
        "ZEEL", "SUNTV", "PVRINOX", "NAZARA", "SAREGAMA", "TIPSMUSIC", "TIPSFILMS"
    ],
    "Nifty Chemicals": [
        "PIDILITIND", "SRF", "UPL", "TATACHEM", "PIIND",
        "DEEPAKNTR", "AARTIIND", "GNFC", "NAVINFLUOR", "ATUL",
        "CLEAN", "VINATIORGA"
    ],
    "Nifty Consumer Durables": [
        "TITAN", "HAVELLS", "VOLTAS", "DIXON", "CROMPTON",
        "WHIRLPOOL", "RAJESHEXPO", "BATAINDIA", "KAJARIACER", "BLUESTARCO"
    ],
    "Nifty Energy": [
        "RELIANCE", "ONGC", "BPCL", "IOC", "NTPC",
        "POWERGRID", "ADANIGREEN", "TATAPOWER", "GAIL", "ADANIPOWER"
    ],
    "Nifty Infra": [
        "LT", "ADANIPORTS", "ULTRACEMCO", "GRASIM", "SHREECEM",
        "AMBUJACEM", "ACC", "SIEMENS", "ABB", "CUMMINSIND",
        "BEL", "HAL", "BHEL"
    ],
    "Nifty Oil & Gas": [
        "RELIANCE", "ONGC", "BPCL", "IOC", "HINDPETRO",
        "GAIL", "OIL", "PETRONET", "IGL", "MGL", "GUJGASLTD"
    ],
    "Nifty India Defence": [
    "HAL", "BEL", "BDL", "MAZDOCK", "COCHINSHIP",
    "GRSE", "MTARTECH", "DATAPATTNS", "BEML"
    ],
}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def _flat_stocks_with_sectors():
    """Returns {symbol: [sector1, sector2, ...]}."""
    out = {}
    for sector, stocks in SECTOR_STOCKS.items():
        for s in stocks:
            out.setdefault(s, []).append(sector)
    return out


def _polite_sleep():
    time.sleep(random.uniform(0.8, 1.6))


def _fetch_max_history(yf_ticker):
    """
    Robust history fetch.
    Some Nifty sectoral indices on yfinance reject period='max'.
    We try a cascade: max → start=20y → 10y → 5y.
    """
    from datetime import datetime, timedelta
    attempts = [
        {"period": "max"},
        {"start": (datetime.now() - timedelta(days=20 * 365)).strftime("%Y-%m-%d")},
        {"start": (datetime.now() - timedelta(days=10 * 365)).strftime("%Y-%m-%d")},
        {"period": "5y"},
        {"period": "1y"},
    ]
    last_err = None
    for kwargs in attempts:
        for retry in range(2):  # 2 retries per kwarg
            try:
                df = yf.download(yf_ticker, interval="1d",
                                 progress=False, auto_adjust=True, **kwargs)
                if df is None or df.empty:
                    break  # try next kwargs
                df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
                return df
            except Exception as e:
                last_err = str(e)
                if retry == 0:
                    time.sleep(2)
                else:
                    break  # try next kwargs
    if last_err:
        print(f"      ❌ All attempts failed: {last_err}")
    return None


# ─────────────────────────────────────────────
# BOOTSTRAP STOCKS
# ─────────────────────────────────────────────
def bootstrap_stocks():
    stocks_map = _flat_stocks_with_sectors()
    total = len(stocks_map)
    print(f"\n📦 Bootstrapping {total} F&O stocks (max history)...")

    job_id = start_job_run("BOOTSTRAP_STOCKS", notes=f"{total} stocks")
    processed = 0
    skipped   = 0
    failed    = 0

    for i, (symbol, sectors) in enumerate(sorted(stocks_map.items()), 1):
        # Register stock in master table
        try:
            upsert_stock(symbol=symbol,
                         yfinance_ticker=f"{symbol}.NS",
                         company_name=None,
                         isin=None)
            upsert_sector_mapping(symbol, sectors)
        except Exception as e:
            print(f"  [{i:3}/{total}] {symbol:<12} ❌ stock upsert: {e}")
            failed += 1
            continue

        # Check if we already have data
        latest = get_latest_trade_date(symbol)
        if latest:
            # Resumable: skip stocks already loaded recently (within 7 days)
            days_old = (datetime.now().date() - latest).days
            if days_old <= 7:
                print(f"  [{i:3}/{total}] {symbol:<12} ⏭️  already loaded (latest: {latest})")
                skipped += 1
                continue

        df = _fetch_max_history(f"{symbol}.NS")
        if df is None or df.empty:
            print(f"  [{i:3}/{total}] {symbol:<12} ⚠️  no data from yfinance")
            failed += 1
            continue

        try:
            rows = insert_daily_prices(symbol, df)
            mark_data_refreshed(symbol)
            print(f"  [{i:3}/{total}] {symbol:<12} ✅ {rows} rows | "
                  f"{df.index.min().date()} → {df.index.max().date()}")
            processed += 1
        except Exception as e:
            print(f"  [{i:3}/{total}] {symbol:<12} ❌ DB insert: {e}")
            failed += 1

        _polite_sleep()

    finish_job_run(job_id, "SUCCESS" if failed == 0 else "PARTIAL",
                   stocks_processed=processed,
                   error_message=f"failed={failed} skipped={skipped}" if (failed or skipped) else None)

    print(f"\n   ✅ Processed: {processed} | Skipped: {skipped} | Failed: {failed}")


# ─────────────────────────────────────────────
# BOOTSTRAP INDICES
# ─────────────────────────────────────────────
def bootstrap_indices():
    total = len(NSE_SECTOR_INDICES)
    print(f"\n📊 Bootstrapping {total} indices...")
    job_id = start_job_run("BOOTSTRAP_INDICES", notes=f"{total} indices")
    processed, failed = 0, 0

    for i, (name, ticker) in enumerate(NSE_SECTOR_INDICES.items(), 1):
        latest = get_latest_index_date(name)
        if latest:
            days_old = (datetime.now().date() - latest).days
            if days_old <= 7:
                print(f"  [{i:2}/{total}] {name:<28} ⏭️  already loaded")
                continue

        df = _fetch_max_history(ticker)
        if df is None or df.empty:
            print(f"  [{i:2}/{total}] {name:<28} ⚠️  no data ({ticker})")
            failed += 1
            continue
        try:
            rows = insert_index_prices(name, df)
            print(f"  [{i:2}/{total}] {name:<28} ✅ {rows} rows | "
                  f"{df.index.min().date()} → {df.index.max().date()}")
            processed += 1
        except Exception as e:
            print(f"  [{i:2}/{total}] {name:<28} ❌ {e}")
            failed += 1
        _polite_sleep()

    finish_job_run(job_id, "SUCCESS" if failed == 0 else "PARTIAL",
                   stocks_processed=processed,
                   error_message=f"failed={failed}" if failed else None)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    args = set(sys.argv[1:])

    print("="*60)
    print("  NSE F&O SCANNER — BOOTSTRAP")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print("="*60)

    # Verify DB connection
    print("\n[0] Verifying DB connection...")
    try:
        v = test_connection()
        print(f"   ✅ {v[:60]}...")
    except Exception as e:
        print(f"   ❌ Cannot connect: {e}")
        sys.exit(1)

    # Initialize schema (idempotent)
    print("\n[1] Initializing schema (idempotent)...")
    try:
        init_schema("schema.sql")
    except Exception as e:
        print(f"   ⚠️  Schema init: {e}")

    # Migrate JSON if exists
    if "--migrate-ca" in args or not args or "--stocks" in args or "--indices" in args:
        print("\n[2] Migrating existing corporate_actions.json (if present)...")
        try:
            migrate_from_json("corporate_actions.json")
        except Exception as e:
            print(f"   ⚠️  Migration: {e}")

    # Indices
    if not args or "--indices" in args:
        bootstrap_indices()

    # Stocks
    if not args or "--stocks" in args:
        bootstrap_stocks()

    # Summary
    print("\n" + "="*60)
    print("DATABASE STATS")
    print("="*60)
    stats = quick_stats()
    for k, v in stats.items():
        print(f"  {k:<20}: {v}")

    print("\n✅ Bootstrap complete.")


if __name__ == "__main__":
    main()
