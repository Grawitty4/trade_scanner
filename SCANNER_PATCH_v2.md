# Scanner Patch v2 — Bulk Freshness Check

This patch makes the scanner avoid yfinance calls for stocks already up-to-date.
Combined with the new connection pool in `db.py`, total runtime should drop from
~20 min to ~2-3 min.

---

## Edit 1: Import the new bulk helper

In `market_scanner.py`, find:

```python
from db import (
    test_connection,
    init_schema,
    get_all_stocks,
    get_stocks_by_sector,
    get_latest_trade_date,
    insert_daily_prices,
    fetch_prices_df,
    get_latest_index_date,
    insert_index_prices,
    fetch_index_df,
    mark_data_refreshed,
    save_scan_result,
    start_job_run,
    finish_job_run,
)
```

Add `get_latest_trade_dates_bulk` and `get_quality_flagged_symbols`:

```python
from db import (
    test_connection,
    init_schema,
    get_all_stocks,
    get_stocks_by_sector,
    get_latest_trade_date,
    get_latest_trade_dates_bulk,
    get_quality_flagged_symbols,
    insert_daily_prices,
    fetch_prices_df,
    get_latest_index_date,
    insert_index_prices,
    fetch_index_df,
    mark_data_refreshed,
    save_scan_result,
    start_job_run,
    finish_job_run,
)
```

---

## Edit 2: Speed up `[2/5] Incremental fetch: stocks`

In `run_full_scan()`, find this block:

```python
    # [2] Incremental fetch — stocks
    print("\n[2/5] Incremental fetch: stocks...")
    all_symbols = get_all_stocks(active_only=True)
    stock_updates = 0
    for sym in all_symbols:
        if sym in quarantined:
            continue
        r = incremental_fetch_stock(sym)
        if r:
            stock_updates += r
        time.sleep(random.uniform(0.2, 0.5))
    print(f"   ✅ Stocks: {stock_updates} new rows across {len(all_symbols)} symbols")
```

Replace with:

```python
    # [2] Incremental fetch — stocks (only fetch when behind)
    print("\n[2/5] Incremental fetch: stocks...")
    all_symbols = get_all_stocks(active_only=True)

    # Skip stocks flagged with quality issues (e.g., LTM with insufficient history)
    flagged_bad = get_quality_flagged_symbols()
    if flagged_bad:
        print(f"   ⏭️  Skipping {len(flagged_bad)} flagged stocks: "
              f"{', '.join(sorted(flagged_bad))}")

    # Bulk lookup of latest dates — one query instead of 200
    today = datetime.now().date()
    latest_dates = get_latest_trade_dates_bulk(all_symbols)

    needs_update = []
    for sym in all_symbols:
        if sym in quarantined or sym in flagged_bad:
            continue
        latest = latest_dates.get(sym)
        if latest is None or (today - latest).days >= 1:
            needs_update.append(sym)

    print(f"   ℹ️  {len(all_symbols) - len(needs_update)} up-to-date, "
          f"{len(needs_update)} need fetch")

    stock_updates = 0
    for sym in needs_update:
        r = incremental_fetch_stock(sym)
        if r:
            stock_updates += r
        time.sleep(random.uniform(0.2, 0.4))

    print(f"   ✅ Stocks: {stock_updates} new rows")
```

---

## Edit 3: Skip flagged stocks in the scan loops too

Find this loop in `run_full_scan()`:

```python
    all_scanned = {}
    for sector in bullish_sectors:
        stocks = get_stocks_by_sector(sector)
        hits = []
        for sym in stocks:
            if sym in quarantined:
                continue
```

Change to:

```python
    all_scanned = {}
    for sector in bullish_sectors:
        stocks = get_stocks_by_sector(sector)
        hits = []
        for sym in stocks:
            if sym in quarantined or sym in flagged_bad:
                continue
```

And similarly find the remaining-stocks loop:

```python
    for sym in all_symbols:
        if sym in all_scanned or sym in quarantined:
            continue
```

Change to:

```python
    for sym in all_symbols:
        if sym in all_scanned or sym in quarantined or sym in flagged_bad:
            continue
```

---

## What this does

| Before | After |
|---|---|
| 166 `get_latest_trade_date()` calls (~400s) | 1 bulk call (~3s) |
| 166 yfinance calls even when up-to-date (~60s) | 0 yfinance calls when up-to-date (~0s) |
| Each DB query = new TCP connection (~2.5s each) | Pooled connections (~50ms each) |

Expected total runtime drop: **20 min → 2-3 min**

---

## Run order

```bash
# 1. Replace db.py with the new pooled version
# 2. Apply the edits in SCANNER_PATCH_v2.md to market_scanner.py
# 3. Optional: prune bad single-row indices
python prune_bad_indices.py

# 4. Run scanner (should be MUCH faster now)
python market_scanner.py
```
