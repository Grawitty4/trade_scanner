# Scanner Patch v5 — Aesthetic Cleanups

Three small edits to `market_scanner.py`:
1. Remove the standalone "Corporate Action Flags" section from the report
2. Bump "Upcoming Corp Actions" window from 7 → 14 days
3. Add WoW and MoM comparisons to the Market Direction section

---

## Edit 1: Remove the "Corporate Action Flags" section

In `format_results()`, find this block:

```python
    if results.get("corp_actions_flagged"):
        lines.append("\n🚩 CORPORATE ACTION FLAGS (review recommended)")
        lines.append("-" * 60)
        for f in results["corp_actions_flagged"]:
            lines.append(f"  {f['symbol']:<15} | {f['action_type']:<10} | ex-date: {f['ex_date']:<12}")
            if f.get("details"):
                lines.append(f"      {f['details'][:80]}")
        lines.append("-" * 60)
```

**Delete it entirely.**

The corp action flagging logic still runs in `run_full_scan()` (populating `results["corp_actions_flagged"]`) — we're only hiding it from the report. If you ever want it back, it's a one-line uncomment.

---

## Edit 2: Bump upcoming window to 14 days

In `run_full_scan()`, find:

```python
    upcoming_7d = get_upcoming_actions(days_ahead=7)
```

Change to:

```python
    upcoming_14d = get_upcoming_actions(days_ahead=14)
```

Then find where it's stored in results:

```python
        "corp_actions_upcoming_7d": upcoming_7d,
```

Change the key name AND the variable:

```python
        "corp_actions_upcoming_14d": upcoming_14d,
```

In `format_results()`, find:

```python
    if results.get("corp_actions_upcoming_7d"):
        lines.append("\n📅 UPCOMING CORP ACTIONS (next 7 days)")
        lines.append("-" * 60)
        for u in results["corp_actions_upcoming_7d"]:
```

Change all three references:

```python
    if results.get("corp_actions_upcoming_14d"):
        lines.append("\n📅 UPCOMING CORP ACTIONS (next 14 days)")
        lines.append("-" * 60)
        for u in results["corp_actions_upcoming_14d"]:
```

---

## Edit 3: Add WoW and MoM comparisons to Market Direction

This change is bigger — we need to compute WoW/MoM percentage changes and show them. Three sub-edits:

### 3a. Update `get_market_direction()` to compute comparisons

Find the function `get_market_direction()`. It currently looks roughly like:

```python
def get_market_direction():
    results = {}
    for name, _ticker in BROADER_INDICES.items():
        df = fetch_index_df(name)
        if df is None or len(df) < 50:
            results[name] = "Unknown"
            continue
        close   = df['Close']
        current = float(close.iloc[-1])
        ema50   = float(compute_ema(close, 50).iloc[-1])
        ema200  = float(compute_ema(close, 200).iloc[-1]) if len(close) >= 200 else None
        rsi     = float(compute_rsi(close).iloc[-1])
        macd_l, sig_l, _ = compute_macd(close)
        macd_bull = float(macd_l.iloc[-1]) > float(sig_l.iloc[-1])

        bull_points = sum([
            current > ema50,
            (current > ema200) if ema200 else True,
            macd_bull,
            rsi > 50,
        ])
        if bull_points >= 3:
            direction = "📈 Bullish"
        elif bull_points == 2:
            direction = "➡️ Neutral"
        else:
            direction = "📉 Bearish"
        results[name] = {
            "direction": direction,
            "price": round(current, 2),
            "rsi": round(rsi, 2),
        }
    return results
```

Replace with this version (only the inner loop body is changed — adds WoW & MoM):

```python
def get_market_direction():
    results = {}
    for name, _ticker in BROADER_INDICES.items():
        df = fetch_index_df(name)
        if df is None or len(df) < 50:
            results[name] = "Unknown"
            continue
        close   = df['Close']
        current = float(close.iloc[-1])
        ema50   = float(compute_ema(close, 50).iloc[-1])
        ema200  = float(compute_ema(close, 200).iloc[-1]) if len(close) >= 200 else None
        rsi     = float(compute_rsi(close).iloc[-1])
        macd_l, sig_l, _ = compute_macd(close)
        macd_bull = float(macd_l.iloc[-1]) > float(sig_l.iloc[-1])

        # Week-on-week and month-on-month % changes
        # We approximate: 5 trading days ≈ 1 week, 21 trading days ≈ 1 month
        def _pct_change(idx_back):
            if len(close) <= idx_back:
                return None
            past = float(close.iloc[-(idx_back + 1)])
            return ((current - past) / past) * 100 if past else None

        wow_pct = _pct_change(5)
        mom_pct = _pct_change(21)

        bull_points = sum([
            current > ema50,
            (current > ema200) if ema200 else True,
            macd_bull,
            rsi > 50,
        ])
        if bull_points >= 3:
            direction = "📈 Bullish"
        elif bull_points == 2:
            direction = "➡️ Neutral"
        else:
            direction = "📉 Bearish"

        results[name] = {
            "direction": direction,
            "price": round(current, 2),
            "rsi": round(rsi, 2),
            "wow_pct": round(wow_pct, 2) if wow_pct is not None else None,
            "mom_pct": round(mom_pct, 2) if mom_pct is not None else None,
        }
    return results
```

### 3b. Update the formatter to display WoW & MoM

In `format_results()`, find:

```python
    lines.append("\n🌐 MARKET DIRECTION")
    lines.append("-" * 30)
    for idx, data in results["market"].items():
        if isinstance(data, dict):
            lines.append(f"  {idx}: {data['direction']}  |  Price: {data['price']}  |  RSI: {data['rsi']}")
        else:
            lines.append(f"  {idx}: {data}")
```

Replace with:

```python
    lines.append("\n🌐 MARKET DIRECTION")
    lines.append("-" * 30)
    for idx, data in results["market"].items():
        if isinstance(data, dict):
            wow = data.get("wow_pct")
            mom = data.get("mom_pct")
            wow_str = f"{wow:+.2f}%" if wow is not None else "N/A"
            mom_str = f"{mom:+.2f}%" if mom is not None else "N/A"

            # Arrows to make trend obvious at a glance
            wow_arrow = "🟢" if (wow or 0) > 0 else ("🔴" if (wow or 0) < 0 else "⚪")
            mom_arrow = "🟢" if (mom or 0) > 0 else ("🔴" if (mom or 0) < 0 else "⚪")

            lines.append(f"  {idx}: {data['direction']}  |  Price: {data['price']}  |  RSI: {data['rsi']}")
            lines.append(f"     WoW: {wow_arrow} {wow_str}   |   MoM: {mom_arrow} {mom_str}")
        else:
            lines.append(f"  {idx}: {data}")
```

---

## Verification

After all 3 edits, run:

```bash
python market_scanner.py
```

Expected differences in output:
- **No more** "🚩 CORPORATE ACTION FLAGS" section at the top
- **"📅 UPCOMING CORP ACTIONS (next 14 days)"** instead of 7 days
- **Market Direction** section now shows 2 lines per index — direction/price/RSI on line 1, WoW/MoM on line 2:

```
🌐 MARKET DIRECTION
------------------------------
  NIFTY 50: 📈 Bullish  |  Price: 26,234.55  |  RSI: 67.21
     WoW: 🟢 +1.43%   |   MoM: 🟢 +3.81%
  SENSEX: 📈 Bullish  |  Price: 86,123.45  |  RSI: 65.10
     WoW: 🟢 +1.21%   |   MoM: 🟢 +3.54%
```

---

## Notes

- **Trading-day approximation**: WoW uses 5 trading days back, MoM uses 21 trading days back. These approximations are standard in trading software and handle weekends/holidays correctly. We're not doing calendar-week math because markets are closed on weekends.

- **Color coding**: 🟢 = positive, 🔴 = negative, ⚪ = exactly zero. These work in most terminals and the Telegram bot we'll build later.

- **The CORP ACTION FLAGS data is still computed** (just not displayed). If you ever want it back as a separate file or alert, the data is sitting in `results["corp_actions_flagged"]` — easy to expose.
