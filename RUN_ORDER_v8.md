# Run Order — Patch v8 (SMA Crossover + Unified Output + Corp Action Cleanup)

## Step 1 — One-time corp action cleanup

```bash
# Preview
python cleanup_corp_actions.py

# Commit
python cleanup_corp_actions.py --commit
```

This will mark stale and adjusted-already corp actions as RESOLVED/STALE.
After this, the NMDC-type false 🚩 badges will disappear.

---

## Step 2 — Apply auto_compute_factors_patch.md

Small edit to `auto_compute_factors.py` so future events automatically clear
the PENDING flag when an adjustment is created. Prevents the same issue from
recurring.

---

## Step 3 — Apply SCANNER_PATCH_v8.md

Four edits to `market_scanner.py`:
1. Add `compute_sma()` and `detect_sma_crossover()` helpers
2. Replace `scan_stock()` body (now scores out of 8, computes RSI-SMA + SMA crossover)
3. Replace `format_stock_block()` (now accepts criteria badges)
4. Replace output sections in `format_results()` with unified F&O / INVESTMENT lists

---

## Step 4 — Verify

```bash
python market_scanner.py
```

Check the output for:
- ✅ Stocks appear ONCE with all matched criteria as badges
- ✅ Two sections: TRADING (F&O) and INVESTMENT (non-F&O)
- ✅ Within each, sorted by breakout score (highest first)
- ✅ RSI-SMA(14) shown for each timeframe
- ✅ SMA 21 Cross line shows intersection price and % distance
- ✅ NMDC (and other already-handled stocks) no longer show 🚩 CORP ACTION

---

## Expected new output format

```
============================================================
⚡ TRADING CANDIDATES (F&O — leveraged + short-sellable) — 14
============================================================

  📌 NMDC @ ₹96.04 | ⚡ AGFS | 📊 SMA Crossover | 🚀 Sector Breakout
     Breakout Score : 6/8
     RSI (D/W/M)    : 65.20 / 68.13 / 62.45
     RSI-SMA(14)    : 60.10 / 65.30 / 58.20
     SMA 21 Cross   : ₹93.18  (Δ +2.86 / +3.07%)
     Entry Type     : Breakout
     Entry Zone     : ₹95.50 – ₹96.50
     Stop Loss      : ₹93.20
     Target 1       : ₹98.80
     Target 2       : ₹101.40
     Risk:Reward    : 1:2.5

  📌 RELIANCE @ ₹2,847.55 | 🎯 GFS
     ...
```

---

## Design notes

**Breakout score is now out of 8.** The new 8th criterion is SMA 21/63 bullish
crossover. Stocks can still qualify for output via GFS, AGFS, score ≥ 4, or
SMA crossover alone.

**SMA Crossover badge logic:** Counts as a +1 score only when SMA-21 crosses
**above** SMA-63 within the last 5 trading days. Bearish crossovers are NOT
flagged (your spec).

**Intersection price:** Per your spec, displayed as today's SMA-21 value.
The Δ shows how far current close is from this value (positive = above SMA-21).

**RSI-SMA(14):** 14-period SMA of the RSI series, shown for daily/weekly/monthly.
Used for visual reference — currently not part of scoring logic. We can wire it
into a "strong trend" criterion later if useful.

**Multiple sector membership:** A stock that qualifies via multiple sectors
now appears only once. It still gets the 🚀 Sector Breakout badge if it
appears in any of the breakout sectors' top-3.
