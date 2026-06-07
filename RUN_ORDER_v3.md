# Run Order — Adjustment System (Final)

## Step 1 — Backfill historical corp actions from NSE (one-time, ~5 mins)

```bash
# Dry-run first to see chunk structure and entry counts
python corp_actions_backfill.py --dry-run --years 5

# Commit
python corp_actions_backfill.py --years 5
```

**What happens:**
- Splits 5 years into 90-day windows
- Walks each window with delays (~3 sec between)
- Inserts into `corporate_actions` table (idempotent — duplicates skipped)
- ~20 windows × ~15 sec each ≈ 5 minutes

**Expected:** A few hundred new corporate action entries, including TATAMOTORS, TIPS, STLTECH, MORGANITE etc. depending on what NSE has on file.

If a window fails (rate-limit, timeout), the script reports it at the end. Just re-run to retry failures — successful inserts won't be duplicated.

---

## Step 2 — Compute factors from the populated corp action table

```bash
# Dry-run to preview which stocks will get factors
python auto_compute_factors.py --dry-run --years 5

# Commit
python auto_compute_factors.py --years 5
```

**What happens:**
- Reads all DEMERGER/MERGER entries from `corporate_actions`
- For each, computes factor = `open_on_event_date / close_on_prev_day` (your formula)
- Skips factors ≥ 0.95 (yfinance likely already handled them)
- Inserts into `corporate_action_adjustments`

**Expected output:**
```
✅ VEDL          DEMERGER   ex=2026-04-30  open ₹289.50 / prev_close ₹773.60 = factor 0.3742
✅ TMPV          DEMERGER   ex=2025-10-24  open ₹...   / prev_close ₹...    = factor 0.XXXX
✅ TIPSMUSIC     DEMERGER   ex=...         ...
⏭️  HINDUNILVR  DEMERGER   ex=2025-12-05  factor=0.9842 — too close to 1.0, skipping
```

---

## Step 3 — Apply the 2 code patches

**3a. `db.py`** — add the new `fetch_prices_df_adjusted` helper (see `db_patch.md`)

**3b. `market_scanner.py`** — one-line change in `load_stock_data` (see `scanner_patch_v4.md`)

---

## Step 4 — Verify

```bash
python check_stock.py VEDL
```

**Expected:** Daily/Weekly/Monthly RSI should now be in healthy ranges that match Chartink/TradingView.

---

## Step 5 — Run full scan

```bash
python market_scanner.py
```

---

## Architecture summary

```
daily_prices                 ← raw yfinance data, never modified
       │
       ├─ corporate_actions  ← all NSE corp action announcements
       │       ↓ (Step 2)
       │
       └─ corporate_action_adjustments  ← computed factors
              ↓
       fetch_prices_df_adjusted()  ← applies factors in memory
              ↓
       scanner → RSI / breakout / patterns
```

**Key property:** Raw prices stay raw. Factors are persistent but the modification happens only in memory at scan time.

---

## When new demergers happen (going forward)

The daily scanner already calls `update_corporate_actions()` which picks up new events from NSE's last 30 days. After the next event:

```bash
# Verify the new event is in corporate_actions table
python -c "from db import get_cursor
with get_cursor() as (_, cur):
    cur.execute(\"SELECT * FROM corporate_actions WHERE action_type='DEMERGER' ORDER BY discovered_at DESC LIMIT 5\")
    for r in cur.fetchall(): print(r)"

# Recompute factors (only NEW ones get inserted)
python auto_compute_factors.py
```

Scanner picks up the new factor on the next run automatically — no code changes.
