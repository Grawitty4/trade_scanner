# NIFTY 500 Expansion — Run Order

## Step 1 — Add stocks to DB

```bash
# Dry-run first
python expand_to_nifty500.py

# Commit
python expand_to_nifty500.py --commit
```

**Expected output:**
- Downloads NSE's CSV (~500 rows)
- ~166 already in DB (your F&O list)
- ~330 new stocks to add (mid + small caps)
- Inserts them with `is_fno=False`
- Auto-creates sector mappings from CSV's Industry column

Should take <1 minute total.

---

## Step 2 — Bootstrap historical data for new stocks

```bash
# Dry-run preview
python bootstrap_new_stocks.py

# Commit with 4 parallel workers (default — safe for yfinance)
python bootstrap_new_stocks.py --commit
```

**Expected:**
- Identifies ~330 stocks needing data
- Parallel fetch with progress updates every 20 stocks
- Total time: ~15-25 minutes (depends on yfinance responsiveness)
- ~5-15 stocks will likely fail (delisted, rebranded, missing on Yahoo) — fine, scanner skips them

If yfinance throttles, reduce workers:
```bash
python bootstrap_new_stocks.py --commit --workers 2
```

---

## Step 3 — Apply scanner patch v7 (segregation)

Follow `SCANNER_PATCH_v7.md` to add F&O vs Investment segregation to the output.

---

## Step 4 — Run scanner

```bash
python market_scanner.py
```

**What to expect:**
- Runtime: probably 60-75 min (you accepted this)
- Output now has two sections:
  - ⚡ TRADING CANDIDATES (F&O)
  - 📈 INVESTMENT CANDIDATES (non-F&O NIFTY 500)
- Each stock tagged with `📈 INV` if it's an investment-only pick

---

## What happens after

Tomorrow we tackle the runtime properly (Path A from yesterday). After that:
- Runtime drops to ~3-5 min for the full 500-stock universe
- We're then ready for Phase 3: Telegram bot + Railway deployment

---

## Failure recovery

**If `expand_to_nifty500.py` fails:**
- NSE may be blocking; try again later
- The script is idempotent; re-running picks up where it left off

**If `bootstrap_new_stocks.py` fails partway:**
- Idempotent: re-run and it skips stocks already loaded
- For persistent failures on specific stocks, the symbols are listed in the summary — likely delisted/renamed; you can verify manually

**If scanner runtime is unbearable today:**
- Run with `--limit` (Phase 2 doesn't add a CLI limit, but you can manually pause)
- Or skip the daily run until tomorrow's perf fix
