# Scanner Patch v9 — Major Logic Update

Implements items 1, 3, 4, 5, 6, 7, 8 from your update list.
Item 2 (F&O sync) is handled by the standalone `sync_fno_list.py` script.

Order of edits below mirrors complexity (simplest first).

---

## Edit 1 (Item 8): Fix AGFS daily RSI range to 60-65

Find this line in `scan_stock()`:

```python
    agfs = (rsi_monthly > 60 and rsi_weekly > 60 and rsi_daily > 60)
```

Replace with:

```python
    agfs = (rsi_monthly > 60 and rsi_weekly > 60 and 60 < rsi_daily <= 65)
```

---

## Edit 2 (Item 5): Remove Entry/SL/Target from output (keep in DB)

In `format_stock_block()`, find and DELETE these 6 lines:

```python
        f"     Entry Type     : {stock['entry_type']}",
        f"     Entry Zone     : {stock['entry_zone']}",
        f"     Stop Loss      : {stock['stop_loss']}",
        f"     Target 1       : {stock['target1']}",
        f"     Target 2       : {stock['target2']}",
        f"     Risk:Reward    : 1:{stock['rr_ratio']}",
```

We'll add the Elliott Wave line in their place in Edit 8.

The `scan_stock()` function and `save_scan_result()` are unchanged — entry/SL/target data is still computed and persisted to the DB, just not displayed.

---

## Edit 3 (Item 6): Add RSI-SMA scoring criterion (score becomes /9)

### 3a. Add RSI-SMA color helper near the top of the file:

```python
def rsi_sma_zone(rsi_value, rsi_sma_value):
    """
    Return ('green'|'yellow'|'red'|None) zone for RSI vs its SMA.
      green : RSI > RSI_SMA + 2
      yellow: RSI_SMA - 2 <= RSI <= RSI_SMA + 2
      red   : RSI < RSI_SMA - 2
    """
    if rsi_value is None or rsi_sma_value is None:
        return None
    diff = rsi_value - rsi_sma_value
    if diff > 2:
        return "green"
    if diff < -2:
        return "red"
    return "yellow"


def zone_emoji(zone):
    return {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(zone, "⚪")
```

### 3b. In `scan_stock()`, after the RSI-SMA values are computed but BEFORE the breakout-score block, add:

```python
    # RSI vs RSI-SMA zones for each timeframe
    zone_d = rsi_sma_zone(rsi_daily,   rsi_d_sma_val)
    zone_w = rsi_sma_zone(rsi_weekly,  rsi_w_sma_val)
    zone_m = rsi_sma_zone(rsi_monthly, rsi_m_sma_val)
    all_three_green = (zone_d == "green" and zone_w == "green" and zone_m == "green")
```

### 3c. Append a 9th criterion in the score block. Find the last existing criterion (SMA 21/63 Crossover):

```python
    c8 = bool(sma_cross["crossover"])
    score += int(c8); score_detail["SMA 21/63 Crossover"] = c8
```

Add right after:

```python
    # 9th criterion: RSI above its 14-period SMA on ALL three timeframes (strict)
    c9 = all_three_green
    score += int(c9); score_detail["RSI > RSI-SMA (D+W+M)"] = c9
```

### 3d. Update the return dict to include the zones:

```python
    return {
        ...
        "rsi_daily_zone":   zone_d,
        "rsi_weekly_zone":  zone_w,
        "rsi_monthly_zone": zone_m,
        ...
    }
```

### 3e. Change score denominator. Find:

```python
        "breakout_score": f"{score}/8",
```

Replace with:

```python
        "breakout_score": f"{score}/9",
```

### 3f. Update `format_stock_block()` to colorize RSI display

Find the existing RSI / RSI-SMA lines:

```python
    lines.append(
        f"     RSI (D/W/M)    : {stock['rsi_daily']} / {stock['rsi_weekly']} / {stock['rsi_monthly']}"
    )
    d_sma = stock.get("rsi_daily_sma")
    ...
```

Replace with:

```python
    # Color-coded RSI display based on RSI-SMA zones
    zd = stock.get("rsi_daily_zone")
    zw = stock.get("rsi_weekly_zone")
    zm = stock.get("rsi_monthly_zone")
    emoji_d = zone_emoji(zd)
    emoji_w = zone_emoji(zw)
    emoji_m = zone_emoji(zm)

    lines.append(
        f"     RSI (D/W/M)    : {emoji_d} {stock['rsi_daily']} / "
        f"{emoji_w} {stock['rsi_weekly']} / "
        f"{emoji_m} {stock['rsi_monthly']}"
    )
    d_sma = stock.get("rsi_daily_sma")
    w_sma = stock.get("rsi_weekly_sma")
    m_sma = stock.get("rsi_monthly_sma")
    if any(v is not None for v in [d_sma, w_sma, m_sma]):
        def _v(x): return f"{x}" if x is not None else "—"
        lines.append(f"     RSI-SMA(14)    : {_v(d_sma)} / {_v(w_sma)} / {_v(m_sma)}")
```

---

## Edit 4 (Item 4): New sector breakout logic — RSI-based week-on-week

Find `scan_sector_direction()`. Replace the entire function with:

```python
def scan_sector_direction():
    """
    New sector classification (RSI-based, week-over-week):
      • Last completed week's Friday RSI > 60 AND this week's current RSI > last week's → 🟢 Bullish
      • Last completed week's RSI > 60 AND this week's RSI <= last week's → 🟡 Amber (cooling)
      • Last completed week's RSI <= 60 → 🔴 Bearish
    """
    out = {}
    for sector in NSE_SECTOR_INDICES.keys():
        df = fetch_index_df(sector)
        source = "real"
        if df is None or len(df) < 30:
            df = fetch_index_df(sector + " (synth)")
            source = "synth"
        if df is None or len(df) < 30:
            out[sector] = {"status": "Data Unavailable", "rsi": 0, "source": "none"}
            continue

        close = df['Close']

        # Resample to weekly (Mon–Fri grouped on Friday close)
        weekly = df.resample("W").agg({
            "Open": "first", "High": "max", "Low": "min",
            "Close": "last", "Volume": "sum",
        }).dropna()

        if len(weekly) < 20:
            # Fall back to daily RSI based status
            rsi = float(compute_rsi(close).iloc[-1])
            out[sector] = {"status": "Insufficient", "rsi": round(rsi, 2), "source": source,
                           "rsi_last_week": None, "rsi_this_week": None}
            continue

        rsi_weekly = compute_rsi(weekly['Close'])
        # The current-week row is the most recent in `weekly` (its Close is the
        # latest available daily close — i.e., "this week so far").
        rsi_this_week = float(rsi_weekly.iloc[-1])
        rsi_last_week = float(rsi_weekly.iloc[-2]) if len(rsi_weekly) >= 2 else None

        if rsi_last_week is None:
            status = "Insufficient"
        elif rsi_last_week > 60 and rsi_this_week > rsi_last_week:
            status = "Bullish Breakout"
        elif rsi_last_week > 60 and rsi_this_week <= rsi_last_week:
            status = "Cooling (Amber)"
        else:
            status = "Bearish"

        out[sector] = {
            "status":         status,
            "rsi":            round(rsi_this_week, 2),    # backward-compat
            "rsi_last_week":  round(rsi_last_week, 2) if rsi_last_week is not None else None,
            "rsi_this_week":  round(rsi_this_week, 2),
            "source":         source,
        }
    return out
```

### Update the sector formatter

Find this block in `format_results()`:

```python
    lines.append("\n📂 SECTOR STATUS")
    lines.append("-" * 30)
    for sector, data in results["sectors"].items():
        if isinstance(data, dict):
            status = data.get("status", "Unknown")
            emoji = {"Bullish Breakout": "🟢", "Neutral": "🟡", "Bearish": "🔴"}.get(status, "⚪")
            src   = data.get("source", "?")
            src_marker = "" if src == "real" else f" [{src}]"
            lines.append(f"  {emoji} {sector}{src_marker}: {status}  (RSI: {data.get('rsi', 'N/A')})")
```

Replace with:

```python
    lines.append("\n📂 SECTOR STATUS (Weekly RSI momentum)")
    lines.append("-" * 30)
    status_emoji = {
        "Bullish Breakout": "🟢",
        "Cooling (Amber)":  "🟡",
        "Bearish":          "🔴",
        "Insufficient":     "⚪",
        "Data Unavailable": "⚪",
    }
    for sector, data in results["sectors"].items():
        if isinstance(data, dict):
            status = data.get("status", "Unknown")
            emoji = status_emoji.get(status, "⚪")
            src   = data.get("source", "?")
            src_marker = "" if src == "real" else f" [{src}]"
            lw = data.get("rsi_last_week")
            tw = data.get("rsi_this_week")
            if lw is not None and tw is not None:
                lines.append(
                    f"  {emoji} {sector}{src_marker}: {status}  "
                    f"(LW RSI: {lw} → TW RSI: {tw})"
                )
            else:
                lines.append(f"  {emoji} {sector}{src_marker}: {status}")
```

### Update bullish-sector filter

Find:
```python
    bullish_sectors = [s for s, v in sectors.items() if v.get("status") == "Bullish Breakout"]
```
No change needed — the string "Bullish Breakout" is preserved.

---

## Edit 5 (Item 3): Market Direction with arrows

In `get_market_direction()`, replace the existing WoW/MoM logic with this version that computes prev-period close and current-period close:

Find:
```python
        # Week-on-week and month-on-month % changes
        ...
        wow_pct = _pct_change(5)
        mom_pct = _pct_change(21)
```

Replace with:

```python
        # Period-based comparisons:
        #   prev_week_close = close on last trading day of previous calendar week
        #   curr_close      = today's close (last row of daily series)
        #   prev_month_close = close on last trading day of previous calendar month
        # Trend arrow per period: ⬆️ if end > start, ⬇️ if end < start, ➡️ if equal
        # Where "start" = close at end of PREVIOUS period, "end" = close at end of THIS period

        # Resample daily to weekly close (last close of each calendar week)
        weekly_closes  = close.resample("W").last().dropna()
        monthly_closes = close.resample("ME").last().dropna()

        def _arrow(end_val, start_val):
            if end_val is None or start_val is None:
                return "➡️"
            if end_val > start_val:  return "⬆️"
            if end_val < start_val:  return "⬇️"
            return "➡️"

        # LAST WEEK: completed week (the second-to-last row when running mid-week)
        # In pandas, the last entry in weekly_closes is "current week so far" if today
        # isn't Friday, OR the completed current week if today is Friday.
        # Either way: index -1 = current period, index -2 = previous completed period.
        last_week_close = float(weekly_closes.iloc[-2]) if len(weekly_closes) >= 2 else None
        prev_week_close = float(weekly_closes.iloc[-3]) if len(weekly_closes) >= 3 else None
        last_month_close = float(monthly_closes.iloc[-2]) if len(monthly_closes) >= 2 else None
        prev_month_close = float(monthly_closes.iloc[-3]) if len(monthly_closes) >= 3 else None

        this_week_close  = float(weekly_closes.iloc[-1]) if len(weekly_closes) else None
        this_month_close = float(monthly_closes.iloc[-1]) if len(monthly_closes) else None
```

Update the result dict assembly:

Find:
```python
        results[name] = {
            "direction": direction,
            "price": round(current, 2),
            "rsi": round(rsi, 2),
            "wow_pct": round(wow_pct, 2) if wow_pct is not None else None,
            "mom_pct": round(mom_pct, 2) if mom_pct is not None else None,
        }
```

Replace with:

```python
        results[name] = {
            "direction":         direction,
            "price":             round(current, 2),
            "rsi":               round(rsi, 2),
            # Previous completed week: prev_week_close → last_week_close
            "last_week":  {
                "start_close": round(prev_week_close, 2) if prev_week_close is not None else None,
                "end_close":   round(last_week_close, 2) if last_week_close is not None else None,
                "arrow":       _arrow(last_week_close, prev_week_close),
            },
            # Current (in-progress) week: last_week_close → this_week_close
            "this_week":  {
                "start_close": round(last_week_close, 2) if last_week_close is not None else None,
                "end_close":   round(this_week_close, 2) if this_week_close is not None else None,
                "arrow":       _arrow(this_week_close, last_week_close),
                "in_progress": True,
            },
            "last_month": {
                "start_close": round(prev_month_close, 2) if prev_month_close is not None else None,
                "end_close":   round(last_month_close, 2) if last_month_close is not None else None,
                "arrow":       _arrow(last_month_close, prev_month_close),
            },
            "this_month": {
                "start_close": round(last_month_close, 2) if last_month_close is not None else None,
                "end_close":   round(this_month_close, 2) if this_month_close is not None else None,
                "arrow":       _arrow(this_month_close, last_month_close),
                "in_progress": True,
            },
        }
```

### Update Market Direction formatter

Find:
```python
    lines.append("\n🌐 MARKET DIRECTION")
    lines.append("-" * 30)
    for idx, data in results["market"].items():
        if isinstance(data, dict):
            wow = data.get("wow_pct")
            mom = data.get("mom_pct")
            wow_str = f"{wow:+.2f}%" if wow is not None else "N/A"
            mom_str = f"{mom:+.2f}%" if mom is not None else "N/A"
            wow_arrow = "🟢" if (wow or 0) > 0 else ("🔴" if (wow or 0) < 0 else "⚪")
            mom_arrow = "🟢" if (mom or 0) > 0 else ("🔴" if (mom or 0) < 0 else "⚪")
            lines.append(f"  {idx}: {data['direction']}  |  Price: {data['price']}  |  RSI: {data['rsi']}")
            lines.append(f"     WoW: {wow_arrow} {wow_str}   |   MoM: {mom_arrow} {mom_str}")
        else:
            lines.append(f"  {idx}: {data}")
```

Replace with:

```python
    lines.append("\n🌐 MARKET DIRECTION")
    lines.append("-" * 30)
    for idx, data in results["market"].items():
        if isinstance(data, dict):
            lines.append(f"  {idx}: {data['direction']}  |  Price: {data['price']}  |  RSI: {data['rsi']}")

            def _fmt_period(label, period_data, suffix=""):
                if not period_data:
                    return f"     {label:<11}: data unavailable"
                s = period_data.get("start_close")
                e = period_data.get("end_close")
                a = period_data.get("arrow", "➡️")
                if s is None or e is None:
                    return f"     {label:<11}: data unavailable"
                return f"     {label:<11}: {a} {s:,.2f} → {e:,.2f}{suffix}"

            lines.append(_fmt_period("Last week",  data.get("last_week")))
            lines.append(_fmt_period("This week",  data.get("this_week"),  " (in progress)"))
            lines.append(_fmt_period("Last month", data.get("last_month")))
            lines.append(_fmt_period("This month", data.get("this_month"), " (in progress)"))
        else:
            lines.append(f"  {idx}: {data}")
```

---

## Edit 6 (Item 1): Split scanner output into 2 files (daily + Monday corp action)

### 6a. Modify the `__main__` block at the bottom of `market_scanner.py`

Find the existing block:
```python
if __name__ == "__main__":
    try:
        test_connection()
    except Exception as e:
        ...

    results = run_full_scan()
    report  = format_results(results)
    print("\n\n")
    print(report)

    filename = f"scan_result_{datetime.now().strftime('%d_%b_%Y')}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n✅ Report saved to {filename}")
```

Replace with:

```python
def format_corp_action_report(results):
    """Standalone corp action report for Monday emissions."""
    lines = [
        "=" * 60,
        "📅 CORPORATE ACTIONS — NEXT 14 DAYS",
        datetime.now().strftime("Generated %d %b %Y | %I:%M %p"),
        "=" * 60,
    ]
    upcoming = results.get("corp_actions_upcoming_14d", [])
    if not upcoming:
        lines.append("\n   No corporate actions in the next 14 days.")
    else:
        lines.append(f"\n   {len(upcoming)} action(s) in the next 14 days:\n")
        lines.append("-" * 60)
        for u in upcoming:
            risky = " 🚩" if u.get("is_risky") else ""
            lines.append(f"  {u['ex_date']:<12} | {u['symbol']:<14} | {u['action_type']:<10}{risky}")
            if u.get("details"):
                lines.append(f"      {u['details'][:80]}")
        lines.append("-" * 60)

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        raise SystemExit(1)

    results = run_full_scan()
    report  = format_results(results)
    print("\n\n")
    print(report)

    # Daily scan output
    daily_name = f"scan_result_{datetime.now().strftime('%d_%b_%Y')}.txt"
    with open(daily_name, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n✅ Daily report saved to {daily_name}")

    # Monday-only corp actions output
    today = datetime.now()
    if today.weekday() == 0:   # Monday
        corp_name = f"corp_actions_{today.strftime('%d_%b_%Y')}.txt"
        with open(corp_name, "w", encoding="utf-8") as f:
            f.write(format_corp_action_report(results))
        print(f"📅 Monday corp actions report saved to {corp_name}")
    else:
        days_until_monday = (7 - today.weekday()) % 7 or 7
        print(f"ℹ️  Corp action report runs on Mondays (next in {days_until_monday} day(s))")
```

### 6b. Remove the inline corp-actions block from `format_results()`

Find this block in `format_results()`:

```python
    if results.get("corp_actions_upcoming_14d"):
        lines.append("\n📅 UPCOMING CORP ACTIONS (next 14 days)")
        lines.append("-" * 60)
        for u in results["corp_actions_upcoming_14d"]:
            risky = " 🚩" if u.get("is_risky") else ""
            lines.append(f"  {u['ex_date']:<12} | {u['symbol']:<12} | {u['action_type']:<10}{risky}")
            if u.get("details"):
                lines.append(f"      {u['details'][:80]}")
        lines.append("-" * 60)
```

DELETE it entirely. Corp actions now only appear in the separate Monday file.

---

## Edit 7 (Item 7): Heuristic Elliott Wave phase detection

This is the experimental addition. We keep the existing `detect_elliott_wave3()` (used for the score) AND add a new function for phase labeling (display-only).

### 7a. Add the new function

```python
def label_elliott_phase(df, lookback=120):
    """
    Heuristic Elliott Wave phase labeling.
    Identifies recent swing pivots and labels them W1/W2/W3/W4/W5/A/B/C.

    Returns dict:
      - current_phase: '1'|'2'|'3'|'4'|'5'|'A'|'B'|'C'|'?'
      - phases: list of dicts {label, start_date, start_price, end_date, end_price}

    HONESTY NOTE: Elliott Wave labeling is subjective. This is a peak-trough
    heuristic, not authoritative analysis. Treat as a directional hint.
    """
    import numpy as np
    out = {"current_phase": "?", "phases": []}
    if df is None or len(df) < lookback:
        return out

    closes = df['Close'].iloc[-lookback:].values
    dates  = df.index[-lookback:]

    # Identify swing pivots using a simple zigzag (minimum 5% move filter)
    pivots = []  # list of (idx, price, type) where type is 'H' or 'L'
    min_move = 0.05  # 5% threshold

    last_pivot_idx = 0
    last_pivot_price = closes[0]
    direction = None  # 'up' or 'down', set on first significant move

    for i in range(1, len(closes)):
        change = (closes[i] - last_pivot_price) / last_pivot_price
        if direction is None:
            if abs(change) >= min_move:
                direction = "up" if change > 0 else "down"
                pivots.append((last_pivot_idx, last_pivot_price, "L" if direction == "up" else "H"))
        elif direction == "up":
            if closes[i] > last_pivot_price:
                last_pivot_idx, last_pivot_price = i, closes[i]
            elif (last_pivot_price - closes[i]) / last_pivot_price >= min_move:
                pivots.append((last_pivot_idx, last_pivot_price, "H"))
                direction = "down"
                last_pivot_idx, last_pivot_price = i, closes[i]
        else:  # down
            if closes[i] < last_pivot_price:
                last_pivot_idx, last_pivot_price = i, closes[i]
            elif (closes[i] - last_pivot_price) / last_pivot_price >= min_move:
                pivots.append((last_pivot_idx, last_pivot_price, "L"))
                direction = "up"
                last_pivot_idx, last_pivot_price = i, closes[i]

    # Always append the last point as a tentative pivot
    pivots.append((len(closes) - 1, closes[-1],
                   "H" if direction == "up" else "L" if direction == "down" else "?"))

    if len(pivots) < 3:
        return out

    # Take the LAST up to 8 pivots and label
    # Elliott motive: L H L H L H (5 waves) then A(L) B(H) C(L) correction
    # We label the most recent leg as the "current phase"
    # Simple mapping based on count and direction
    recent = pivots[-8:] if len(pivots) >= 8 else pivots
    labels = []
    if len(recent) >= 6:
        labels = ["1", "2", "3", "4", "5", "A", "B", "C"][:len(recent)-1]
    elif len(recent) >= 4:
        labels = ["1", "2", "3", "4", "5"][:len(recent)-1]
    else:
        labels = ["?"] * (len(recent) - 1)

    phases = []
    for i, label in enumerate(labels):
        start = recent[i]
        end   = recent[i + 1]
        phases.append({
            "label":       label,
            "start_date":  dates[start[0]].date(),
            "start_price": float(start[1]),
            "end_date":    dates[end[0]].date(),
            "end_price":   float(end[1]),
        })

    out["phases"] = phases
    out["current_phase"] = labels[-1] if labels else "?"
    return out
```

### 7b. Call it in `scan_stock()` and add to return dict

In `scan_stock()`, after the `entry = determine_entry(daily)` line, add:

```python
    elliott = label_elliott_phase(daily)
```

Then add to the return dict:

```python
        "elliott_phase":  elliott["current_phase"],
        "elliott_phases": elliott["phases"],
```

### 7c. Render in `format_stock_block()`

Right after the SMA Cross line block (before the deleted Entry Type line — which was just removed in Edit 2), add:

```python
    # Elliott Wave (heuristic)
    elliott_phase  = stock.get("elliott_phase")
    elliott_phases = stock.get("elliott_phases", [])
    if elliott_phase and elliott_phase != "?":
        lines.append(f"     Elliott Wave   : currently in phase {elliott_phase}")
        for p in elliott_phases:
            lines.append(
                f"        W{p['label']}: {p['start_date']} ₹{p['start_price']:.2f} "
                f"→ {p['end_date']} ₹{p['end_price']:.2f}"
            )
```

---

## Verification after all edits

Run:
```bash
python market_scanner.py
```

Expect:
- Two output files on Mondays (`scan_result_*.txt` + `corp_actions_*.txt`), one on other days
- Score is now /9
- RSI display has color emojis (🟢/🟡/🔴) per timeframe
- Sector status shows last-week vs this-week RSI
- Market direction shows 4 period rows with arrows
- No Entry/SL/Target in output (still in DB)
- Elliott Wave phase block under each qualifying stock
- AGFS now requires daily RSI in 60-65 range
