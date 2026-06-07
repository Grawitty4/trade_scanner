# Scanner Patch v4 — Use Adjusted Prices

A 2-line change in `market_scanner.py` so the scanner reads adjusted prices.

---

## Edit 1: Import the new helper

Find the existing import block and add `fetch_prices_df_adjusted`:

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
    fetch_prices_df_adjusted,   # ← NEW
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

## Edit 2: Use it in load_stock_data

Find `load_stock_data()` and change the first inner line:

```python
def load_stock_data(symbol):
    """Returns (daily, weekly, monthly) DataFrames or (None, None, None)."""
    daily = fetch_prices_df_adjusted(symbol)   # ← was fetch_prices_df
    if daily is None or len(daily) < 60:
        return None, None, None
    ...
```

---

That's it. The scanner now:
- Reads raw prices from DB
- Applies any factors from `corporate_action_adjustments` in memory
- Computes RSI / patterns / breakout on the adjusted data
- `daily_prices` table remains untouched

Performance cost: ~5-10ms per stock for the multiplication (negligible).
