# Run Order — Corporate Action Adjustments

Follow these steps in order. Each is safe and idempotent.

## Step 1 — Apply the schema (new table)

```bash
# Open Railway Postgres web SQL console and run schema_adjustments.sql contents
# OR if you've used psql locally:
psql "$DATABASE_URL" -f schema_adjustments.sql
```

This creates the `corporate_action_adjustments` table. Safe to run multiple times.

## Step 2 — Seed the 3 known events

```bash
python seed_adjustments.py
```

Pre-populates VEDL, HINDUNILVR, BAJFINANCE. Existing entries are skipped.

## Step 3 — Preview the adjustments (DRY-RUN)

```bash
python apply_adjustments.py
```

Shows what would change WITHOUT modifying data. Verify the numbers look right.

Expected output:
```
VEDL        | DEMERGER  | eff: 2026-04-30 | factor: 0.5234 | rows to adjust: ~7600
   Before adj  (2026-04-29): close = ₹450.00  →  adjusted: ₹235.53
   First post-event (2026-04-30): close = ₹235.00  (unchanged)
   (dry-run — no changes made)

HINDUNILVR  | DEMERGER  | eff: 2025-12-05 | factor: 0.97 | rows to adjust: ~7000
   ...

BAJFINANCE  | COMBO     | eff: 2025-06-16 | factor: 0.10 | rows to adjust: ~7500
   Before adj  (2025-06-13): close = ₹9000.00  →  adjusted: ₹900.00
   First post-event (2025-06-16): close = ₹920.00  (unchanged)
```

## Step 4 — Commit the adjustments

If the dry-run looked correct:

```bash
python apply_adjustments.py --commit
```

This modifies `daily_prices` in place. ~25,000 rows updated total.

## Step 5 — Verify

```bash
python check_stock.py VEDL
```

VEDL's RSI should now be in the GFS/AGFS-eligible range (assuming actual market conditions place it there).

Same for HINDUNILVR and BAJFINANCE.

## Step 6 — Apply scanner patch v3

Make the 4 edits in `SCANNER_PATCH_v3.md`. After this:

- Stocks with pending (unapplied) adjustments get a `⚠️ DATA UNADJUSTED` marker in the report
- Once you `--commit`, the marker disappears

## Step 7 — Run the scanner

```bash
python market_scanner.py
```

Future demergers/splits: add to `seed_adjustments.py` SEED list (or insert directly into the table), then run the same Step 3 → Step 4.

---

## Schema reference

```
corporate_action_adjustments
├── id (PK)
├── symbol            ← which stock
├── action_type       ← DEMERGER, SPLIT, BONUS, COMBO
├── effective_date    ← all prices BEFORE this date get adjusted
├── price_factor      ← multiply old prices by this (e.g., 0.5234)
├── volume_factor     ← multiply old volumes by this (e.g., 10.0 for 10x split)
├── notes             ← human-readable explanation
├── discovered_at     ← when row was added
├── applied_at        ← NULL until apply_adjustments runs
└── applied_by_job_id ← FK to job_runs.id
```

The scanner reads from a view `pending_adjustments` which shows only unapplied rows.

---

## Adding new events later

When a new demerger/split hits, just add an entry:

```python
# In seed_adjustments.py SEED list
{
    "symbol":         "TATAMOTORS",
    "action_type":    "DEMERGER",
    "effective_date": "2025-10-24",
    "price_factor":   0.65,    # determined from cost apportionment
    "volume_factor":  1.0,
    "notes":          "Demerger of PV from CV business..."
},
```

Then run:
```bash
python seed_adjustments.py
python apply_adjustments.py --commit
```

The scanner picks up the change on the next run.

---

## What if an adjustment was wrong?

Re-fetch the stock from yfinance (which restores original prices):
```bash
python refresh_stock.py SYMBOL --force
```

Update the SEED entry with the correct factor, re-run:
```bash
python seed_adjustments.py
python apply_adjustments.py --commit
```
