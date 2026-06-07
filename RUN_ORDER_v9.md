# Run Order — v9 Major Update

## Step 1 — Sync F&O list from NSE

```bash
# Preview
python sync_fno_list.py

# Commit
python sync_fno_list.py --commit
```

Expected: ~50 new F&O symbols added (216 - 166), some promotions/demotions.

---

## Step 2 — Bootstrap any newly-added stocks

If Step 1 added brand new symbols not previously in DB:

```bash
python bootstrap_new_stocks.py --commit
```

---

## Step 3 — Apply SCANNER_PATCH_v9.md

7 edits to `market_scanner.py`. They're independent — you can apply in any order, but I recommend top to bottom.

If you want to test incrementally:
- After Edit 1: AGFS range tightens (rerun scanner, verify fewer AGFS signals)
- After Edit 3: Score becomes /9, RSI lines get color emojis
- After Edit 4: Sector status changes radically (week-RSI based)
- After Edit 5: Market direction display expands
- After Edit 6: Two output files on Mondays
- After Edit 7: Elliott Wave phase block appears

---

## Step 4 — Verify

```bash
python market_scanner.py
```

Read through the output and look for:
- ✅ Scores showing as /9 not /8
- ✅ RSI rows with 🟢/🟡/🔴 markers
- ✅ Sector section with "LW RSI → TW RSI"
- ✅ Market direction with 4 period rows + ⬆️⬇️➡️ arrows
- ✅ No Entry Zone / SL / Target lines
- ✅ Elliott Wave phase block under qualifying stocks
- ✅ If today is Monday: separate corp_actions_*.txt file

If today is not Monday, you can still test the corp action format by temporarily changing the day check in __main__ — or just wait until Monday.

---

## Known limitations to flag

**Elliott Wave heuristic:** This is intentionally a simple zigzag-based labeler with a 5% threshold for swing pivots. Accuracy depends heavily on the stock's volatility profile:
- Clean trending stocks → reasonable labels
- Choppy stocks → labels may not match what a human EW analyst would identify
- Sideways stocks → may collapse all pivots into ABC

Treat as a **directional hint**, not authoritative. Backtest before relying on it.

**Market Direction "this week / this month":** When the scanner runs mid-week or mid-month, the "in progress" close is the most recent daily close — not a full period. The arrow shows current direction so far.

**Score discontinuity:** Score is now /9 vs previous /8 vs original /7. The `scan_results` table has rows with different denominators. If/when you reset scan_results, this is the right time to do it (you said you would in testing phase).
