"""
Sector Index Synthesis
───────────────────────
Computes daily OHLCV for sector indices by aggregating their constituent stocks.

Strategy:
- For each constituent stock in a sector, fetch daily prices from DB
- Use equal-weighted aggregation (simple, robust)
- Synthesized sectors are stored under name "<Sector Name> (synth)"
- Scanner is updated separately to prefer real index, fall back to synth

Why equal-weighted (not market-cap weighted):
- We don't reliably have market cap / share count data
- Equal-weighted is robust to corporate actions
- Captures direction (breakout/RSI), which is what the scanner needs

Usage:
    python sector_index.py              # synthesize all sectors
    python sector_index.py "Nifty IT"   # synthesize one sector
"""

import sys
import time
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

from db import (
    test_connection,
    get_cursor,
    get_stocks_by_sector,
    fetch_prices_df,
    insert_index_prices,
    start_job_run,
    finish_job_run,
)

# Sectors to synthesize. We add "(synth)" suffix to keep them distinct
# from any yfinance-sourced indices.
SECTORS = [
    "Nifty Bank", "Nifty Private Bank", "Nifty PSU Bank",
    "Nifty Financial Services", "Nifty IT", "Nifty Pharma",
    "Nifty Healthcare", "Nifty Auto", "Nifty FMCG", "Nifty Metal",
    "Nifty Realty", "Nifty Media", "Nifty Chemicals",
    "Nifty Consumer Durables", "Nifty Energy", "Nifty Infra",
    "Nifty Oil & Gas", "Nifty India Defence",
]

SYNTH_SUFFIX = " (synth)"


def synthesize_sector(sector_name, min_constituents=3):
    """
    Build a daily OHLCV index by equal-weighting constituent stocks.
    Returns: rows_inserted
    """
    constituents = get_stocks_by_sector(sector_name)
    if not constituents:
        print(f"   ⚠️  {sector_name:<28} no constituents in DB")
        return 0
    if len(constituents) < min_constituents:
        print(f"   ⚠️  {sector_name:<28} only {len(constituents)} constituents "
              f"(min {min_constituents}); skipping")
        return 0

    # Fetch each constituent's price series
    series_list = []
    used_count = 0
    for sym in constituents:
        df = fetch_prices_df(sym)
        if df is None or len(df) < 30:
            continue
        df = df.rename(columns={
            "Open": f"{sym}_O", "High": f"{sym}_H",
            "Low":  f"{sym}_L", "Close": f"{sym}_C",
            "Volume": f"{sym}_V",
        })
        series_list.append(df)
        used_count += 1

    if used_count < min_constituents:
        print(f"   ⚠️  {sector_name:<28} only {used_count} stocks with data; skipping")
        return 0

    # Outer join on date so all stocks share an index
    combined = pd.concat(series_list, axis=1).sort_index()

    # Normalize each stock's close to its first close (base 100)
    # This makes equal-weighted aggregation meaningful across price ranges
    norm_closes = []
    for df in series_list:
        close_col = [c for c in df.columns if c.endswith("_C")][0]
        sym_series = df[close_col].copy()
        # Forward-fill small gaps (max 5 days)
        sym_series = sym_series.ffill(limit=5)
        first_valid = sym_series.first_valid_index()
        if first_valid is None:
            continue
        base = sym_series.loc[first_valid]
        if base == 0 or pd.isna(base):
            continue
        norm = (sym_series / base) * 100.0
        norm_closes.append(norm)

    if not norm_closes:
        print(f"   ⚠️  {sector_name:<28} no usable normalized series")
        return 0

    # Equal-weighted average (skip NaNs per row)
    norm_df = pd.concat(norm_closes, axis=1)
    index_close = norm_df.mean(axis=1, skipna=True).dropna()

    if index_close.empty:
        print(f"   ⚠️  {sector_name:<28} aggregated series is empty")
        return 0

    # Build OHLC by using a rolling daily window (use same close as OHLC for simplicity)
    # Since this is a synthesized smooth index, OHL ~= close. Volume = sum.
    out = pd.DataFrame(index=index_close.index)
    out['Close'] = index_close
    out['Open']  = index_close.shift(1).fillna(index_close)
    out['High']  = index_close.rolling(2).max().fillna(index_close)
    out['Low']   = index_close.rolling(2).min().fillna(index_close)
    out['Volume'] = 0  # not meaningful for synthesized

    # Persist under the "(synth)" name
    name = sector_name + SYNTH_SUFFIX
    rows = insert_index_prices(name, out)
    print(f"   ✅ {name:<35} {rows} rows | "
          f"{out.index.min().date()} → {out.index.max().date()} "
          f"| {used_count} constituents")
    return rows


def main():
    print("="*60)
    print("  SECTOR INDEX SYNTHESIS")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print("="*60)

    try:
        v = test_connection()
        print(f"\n✅ Connected: {v[:60]}...")
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return

    targets = sys.argv[1:] or SECTORS

    job_id = start_job_run("SYNTHESIZE_SECTORS")
    total_rows = 0
    succeeded = 0
    failed = 0

    for sector in targets:
        try:
            n = synthesize_sector(sector)
            if n > 0:
                succeeded += 1
                total_rows += n
            else:
                failed += 1
        except Exception as e:
            print(f"   ❌ {sector}: {e}")
            failed += 1
        time.sleep(0.1)

    finish_job_run(job_id, "SUCCESS" if not failed else "PARTIAL",
                   stocks_processed=succeeded,
                   error_message=f"failed={failed}" if failed else None)

    print(f"\n   ✅ Synthesized: {succeeded} | Failed: {failed} | Total rows: {total_rows}")


if __name__ == "__main__":
    main()
