# Scanner Patch v6 — Parallelization

Targets the 20-25 min runtime. Parallelizes two stages:

1. **Incremental yfinance fetch** (stage [2/5])
2. **Stock scanning loops** (stages [4/5] and [5/5])

Expected speedup: **5-7x** → runtime drops from ~25 min → ~4-5 min.

---

## Why this is safe

- Each stock is independent (no shared mutable state during scan)
- DB connection pool is already there (maxconn=8 in db.py) — threads borrow connections
- yfinance is thread-safe for read calls; 8 concurrent requests stays well under rate limits
- Results are merged at the end into the same final dicts

---

## Edit 1: Add the imports

Near the top of `market_scanner.py`, find the existing imports section and add:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed
```

---

## Edit 2: Parallelize the incremental fetch

Find the existing block in `run_full_scan()`:

```python
    stock_updates = 0
    for sym in needs_update:
        r = incremental_fetch_stock(sym)
        if r:
            stock_updates += r
        time.sleep(random.uniform(0.2, 0.4))

    print(f"   ✅ Stocks: {stock_updates} new rows")
```

Replace with:

```python
    stock_updates = 0
    if needs_update:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(incremental_fetch_stock, s): s for s in needs_update}
            for f in as_completed(futures):
                try:
                    r = f.result()
                    if r:
                        stock_updates += r
                except Exception as e:
                    print(f"   ⚠️  Fetch error for {futures[f]}: {e}")
    print(f"   ✅ Stocks: {stock_updates} new rows")
```

The `time.sleep` is gone — parallelism naturally spaces requests; yfinance is fine with 8 concurrent.

---

## Edit 3: Parallelize the sector-based scan loop

Find this block in `run_full_scan()`:

```python
    all_scanned = {}
    for sector in bullish_sectors:
        stocks = get_stocks_by_sector(sector)
        hits = []
        for sym in stocks:
            if sym in quarantined or sym in flagged_bad:
                continue
            if sym in all_scanned:
                if all_scanned[sym]:
                    hits.append(all_scanned[sym])
                continue
            r = scan_stock(sym, flagged_set)
            if r:
                all_scanned[sym] = {"sector": sector, **r}
                hits.append(r)
            else:
                all_scanned[sym] = None
        valid = [h for h in hits if h]
        valid.sort(key=lambda x: int(x["breakout_score"].split("/")[0]), reverse=True)
        if valid:
            results["breakout_stocks"][sector] = valid[:3]
```

Replace with:

```python
    all_scanned = {}
    # Build the set of all unique symbols to scan in bullish sectors (dedup across overlapping sectors)
    sector_to_stocks = {}
    unique_to_scan = set()
    for sector in bullish_sectors:
        stocks = get_stocks_by_sector(sector)
        sector_to_stocks[sector] = stocks
        for sym in stocks:
            if sym in quarantined or sym in flagged_bad:
                continue
            unique_to_scan.add(sym)

    # Parallel scan
    if unique_to_scan:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(scan_stock, s, flagged_set): s for s in unique_to_scan}
            for f in as_completed(futures):
                sym = futures[f]
                try:
                    r = f.result()
                    all_scanned[sym] = r  # may be None
                except Exception as e:
                    print(f"   ⚠️  Scan error for {sym}: {e}")
                    all_scanned[sym] = None

    # Now build the per-sector top-3 lists from the scanned results
    for sector in bullish_sectors:
        hits = []
        for sym in sector_to_stocks.get(sector, []):
            r = all_scanned.get(sym)
            if r and not (sym in quarantined or sym in flagged_bad):
                # Attach the sector this hit came from (a stock can be in multiple sectors,
                # but we display it under whichever sector first led to its inclusion)
                if "sector" not in r:
                    r = {"sector": sector, **r}
                    all_scanned[sym] = r
                hits.append(r)
        hits.sort(key=lambda x: int(x["breakout_score"].split("/")[0]), reverse=True)
        if hits:
            results["breakout_stocks"][sector] = hits[:3]
```

---

## Edit 4: Parallelize the remaining-stocks scan loop

Find this block:

```python
    # GFS/AGFS scan across remaining symbols
    print("\n[5/5] Remaining GFS/AGFS scan...")
    for sym in all_symbols:
        if sym in all_scanned or sym in quarantined or sym in flagged_bad:
            continue
        r = scan_stock(sym, flagged_set)
        if r:
            all_scanned[sym] = {"sector": "Other", **r}
```

Replace with:

```python
    # GFS/AGFS scan across remaining symbols
    print("\n[5/5] Remaining GFS/AGFS scan...")
    remaining = [s for s in all_symbols
                 if s not in all_scanned and s not in quarantined and s not in flagged_bad]

    if remaining:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(scan_stock, s, flagged_set): s for s in remaining}
            for f in as_completed(futures):
                sym = futures[f]
                try:
                    r = f.result()
                    if r:
                        all_scanned[sym] = {"sector": "Other", **r}
                except Exception as e:
                    print(f"   ⚠️  Scan error for {sym}: {e}")
```

---

## Edit 5: Add timing logs so we can see the improvement

In `run_full_scan()`, at the very top of the function (right after the print banner), add:

```python
    import time as _time
    _t0 = _time.time()
    _stage_times = {}
```

Then at the END of each `[N/5]` stage, add a line like:

```python
    _stage_times["incremental_fetch"] = _time.time() - _t0
```

Specifically, add `_stage_times[NAME] = _time.time() - _t0` at the end of stages 1, 2, 3, 4, 5 with names like:
- `corp_actions`
- `incremental_indices`
- `incremental_stocks`
- `market_sectors`
- `stock_scan_loop`
- `remaining_scan_loop`

At the very end of `run_full_scan()` (just before `return results`), add:

```python
    total = _time.time() - _t0
    print("\n⏱  TIMING")
    last = 0
    for stage, t in _stage_times.items():
        elapsed = t - last
        print(f"   {stage:<25} {elapsed:>7.1f}s  (cumulative {t:>7.1f}s)")
        last = t
    print(f"   {'TOTAL':<25} {total:>7.1f}s")
```

This way we can see exactly where time goes after parallelization, and decide if more optimization is needed.

---

## Verification after applying

Run the scanner:

```bash
python market_scanner.py
```

Expected:
- Total runtime: ~4-7 min (was ~25 min)
- The ⏱ TIMING block at the end tells us where time is spent
- Results should be identical (just faster) — same stocks qualify, same RSI, same entry/SL/targets

If you see errors like "too many connections" or HTTP 429 from yfinance, reduce `max_workers` from 8 to 4. The pool size in `db.py` is 8 so 8 workers maxes it out — usually fine.

---

## What we'll learn from the timing logs

If, after parallelization:
- **incremental_stocks dominates** → yfinance is the bottleneck, can't easily improve more
- **stock_scan_loop or remaining_scan_loop dominates** → the per-stock CPU work (RSI computation) is the issue — we can move to SQL-side compute or vectorize
- **market_sectors dominates** → unlikely but solvable

We'll decide Phase 2 strategy based on this. Then we expand to NIFTY 500.
