# Scanner Patch v8 — SMA Crossover, RSI-SMA, Unified Output

Adds three things in one go:
1. **SMA crossover detection** (21/63) → adds +1 to breakout score (now /8)
2. **14-day RSI-SMA** displayed alongside RSI for each timeframe
3. **Deduplicated stock display** — each stock shown once with all matched criteria as badges

---

## Edit 1: Add indicator helpers

After `compute_ema()` (around line 113), add these functions:

```python
def compute_sma(series, period):
    """Simple moving average."""
    return series.rolling(period).mean()


def detect_sma_crossover(df, fast_period=21, slow_period=63, lookback=5):
    """
    Detect bullish SMA crossover: SMA_21 crossing ABOVE SMA_63.
    Returns dict with:
      - crossover: bool
      - cross_date: pd.Timestamp or None (most recent crossover date)
      - sma_fast_now: float (today's SMA-21)
      - sma_slow_now: float (today's SMA-63)
      - intersection_price: float (today's SMA-21, per user spec)
      - diff_abs: float (current_close - intersection_price)
      - diff_pct: float ((diff_abs / intersection_price) * 100)
    """
    out = {
        "crossover": False, "cross_date": None,
        "sma_fast_now": None, "sma_slow_now": None,
        "intersection_price": None, "diff_abs": None, "diff_pct": None,
    }

    if len(df) < slow_period + 5:
        return out

    sma_fast = compute_sma(df['Close'], fast_period)
    sma_slow = compute_sma(df['Close'], slow_period)

    out["sma_fast_now"] = float(sma_fast.iloc[-1])
    out["sma_slow_now"] = float(sma_slow.iloc[-1])
    out["intersection_price"] = out["sma_fast_now"]  # user spec: today's SMA-21

    current = float(df['Close'].iloc[-1])
    out["diff_abs"] = round(current - out["intersection_price"], 2)
    out["diff_pct"] = round((out["diff_abs"] / out["intersection_price"]) * 100, 2) \
                      if out["intersection_price"] else None

    # Bullish crossover: fast crossed above slow within the last `lookback` bars
    # Check each pair of consecutive bars in window
    window = df.iloc[-(lookback + 1):]
    sma_fast_w = sma_fast.iloc[-(lookback + 1):]
    sma_slow_w = sma_slow.iloc[-(lookback + 1):]

    for i in range(1, len(window)):
        prev_below = sma_fast_w.iloc[i-1] <= sma_slow_w.iloc[i-1]
        now_above  = sma_fast_w.iloc[i]   >  sma_slow_w.iloc[i]
        if prev_below and now_above:
            out["crossover"]  = True
            out["cross_date"] = window.index[i]
            break

    return out
```

---

## Edit 2: Update `scan_stock()` to compute SMAs + RSI-SMA + add to score

Find the body of `scan_stock()`. Replace it entirely with this version (the function signature stays the same):

```python
def scan_stock(symbol, flagged_set=None):
    skip_reason = _is_skipped_by_quality_flag(symbol)
    if skip_reason:
        return None
    daily, weekly, monthly = load_stock_data(symbol)
    if daily is None:
        return None

    close_d = daily['Close']

    # RSI series for each timeframe
    rsi_d_series = compute_rsi(close_d)
    rsi_w_series = compute_rsi(weekly['Close'])
    rsi_m_series = compute_rsi(monthly['Close'])

    rsi_daily   = float(rsi_d_series.iloc[-1])
    rsi_weekly  = float(rsi_w_series.iloc[-1])
    rsi_monthly = float(rsi_m_series.iloc[-1])

    # RSI-SMA (14-period smoothing of the RSI) for each timeframe
    rsi_d_sma = compute_sma(rsi_d_series, 14)
    rsi_w_sma = compute_sma(rsi_w_series, 14)
    rsi_m_sma = compute_sma(rsi_m_series, 14)

    rsi_d_sma_val = float(rsi_d_sma.iloc[-1]) if not rsi_d_sma.iloc[-1] != rsi_d_sma.iloc[-1] else None
    rsi_w_sma_val = float(rsi_w_sma.iloc[-1]) if not rsi_w_sma.iloc[-1] != rsi_w_sma.iloc[-1] else None
    rsi_m_sma_val = float(rsi_m_sma.iloc[-1]) if not rsi_m_sma.iloc[-1] != rsi_m_sma.iloc[-1] else None

    # SMA crossover (21/63 on daily)
    sma_cross = detect_sma_crossover(daily, 21, 63)

    # Breakout score (now out of 8)
    score = 0
    score_detail = {}

    swing_high = float(daily['High'].iloc[-50:].max())
    current    = float(close_d.iloc[-1])
    c1 = current >= swing_high * 0.99
    score += int(c1); score_detail["Price≥Resistance"] = c1

    vol_today = float(daily['Volume'].iloc[-1])
    vol_avg20 = float(daily['Volume'].iloc[-21:-1].mean())
    c2 = vol_today >= vol_avg20 * 1.5
    score += int(c2); score_detail["Volume≥1.5x"] = c2

    c3 = detect_bollinger_squeeze_breakout(daily)
    score += int(c3); score_detail["BB Squeeze→Expansion"] = c3

    _, _, hist = compute_macd(close_d)
    c4 = bool(hist.iloc[-1] > 0 and hist.iloc[-2] <= 0)
    score += int(c4); score_detail["MACD Crossover"] = c4

    c5 = detect_cup_and_handle(daily)
    score += int(c5); score_detail["Cup & Handle"] = c5

    c6 = detect_elliott_wave3(daily)
    score += int(c6); score_detail["Elliott Wave 3"] = c6

    c7 = rsi_daily > 60
    score += int(c7); score_detail["RSI>60 (Daily)"] = c7

    # NEW: 8th criterion — SMA-21 crossed above SMA-63
    c8 = bool(sma_cross["crossover"])
    score += int(c8); score_detail["SMA 21/63 Crossover"] = c8

    gfs  = (rsi_monthly > 60 and rsi_weekly > 60 and 40 <= rsi_daily <= 45)
    agfs = (rsi_monthly > 60 and rsi_weekly > 60 and rsi_daily > 60)

    entry = determine_entry(daily)

    # Qualifies if any of these are true (broadened to include SMA crossover by itself)
    if not ((score >= 4) or gfs or agfs or sma_cross["crossover"]):
        return None

    return {
        "ticker":         symbol,
        "current_price":  f"₹{round(current, 2)}",
        "current_price_raw": round(current, 2),
        "breakout_score": f"{score}/8",
        "score_detail":   score_detail,
        "rsi_daily":      round(rsi_daily,   2),
        "rsi_weekly":     round(rsi_weekly,  2),
        "rsi_monthly":    round(rsi_monthly, 2),
        "rsi_daily_sma":   round(rsi_d_sma_val, 2)   if rsi_d_sma_val is not None else None,
        "rsi_weekly_sma":  round(rsi_w_sma_val, 2)   if rsi_w_sma_val is not None else None,
        "rsi_monthly_sma": round(rsi_m_sma_val, 2)   if rsi_m_sma_val is not None else None,
        "sma_crossover": sma_cross["crossover"],
        "sma_intersection_price": round(sma_cross["intersection_price"], 2)
            if sma_cross["intersection_price"] is not None else None,
        "sma_diff_abs": sma_cross["diff_abs"],
        "sma_diff_pct": sma_cross["diff_pct"],
        "gfs":  gfs,
        "agfs": agfs,
        "corp_action_flag": bool(flagged_set and symbol in flagged_set),
        **entry,
    }
```

---

## Edit 3: Replace `format_stock_block()` with badge-based output

Find `format_stock_block()` and replace with:

```python
def format_stock_block(stock, criteria_list=None):
    """
    Render a stock block. criteria_list is a list of badge strings already
    matched (e.g. ['⚡ AGFS', '⚡ Sector Breakout', '⚡ SMA Crossover']).
    """
    seg_badge = "" if stock.get("is_fno", True) else " | 📈 INV"
    ca_warn   = " | 🚩 CORP ACTION" if stock.get("corp_action_flag") else ""

    badges = " | ".join(criteria_list) if criteria_list else ""
    badges_str = f" | {badges}" if badges else ""

    lines = [
        f"  📌 {stock['ticker']} @ {stock['current_price']}{badges_str}{ca_warn}{seg_badge}",
        f"     Breakout Score : {stock['breakout_score']}",
        f"     RSI (D/W/M)    : {stock['rsi_daily']} / {stock['rsi_weekly']} / {stock['rsi_monthly']}",
    ]

    # RSI-SMA row (only show if at least one available)
    d_sma = stock.get("rsi_daily_sma")
    w_sma = stock.get("rsi_weekly_sma")
    m_sma = stock.get("rsi_monthly_sma")
    if any(v is not None for v in [d_sma, w_sma, m_sma]):
        def _v(x): return f"{x}" if x is not None else "—"
        lines.append(f"     RSI-SMA(14)    : {_v(d_sma)} / {_v(w_sma)} / {_v(m_sma)}")

    # SMA crossover info if present
    if stock.get("sma_crossover"):
        inter = stock.get("sma_intersection_price")
        diff_abs = stock.get("sma_diff_abs")
        diff_pct = stock.get("sma_diff_pct")
        sign_abs = f"{diff_abs:+.2f}" if diff_abs is not None else "—"
        sign_pct = f"{diff_pct:+.2f}%" if diff_pct is not None else "—"
        lines.append(f"     SMA 21 Cross   : ₹{inter}  (Δ {sign_abs} / {sign_pct})")

    lines.extend([
        f"     Entry Type     : {stock['entry_type']}",
        f"     Entry Zone     : {stock['entry_zone']}",
        f"     Stop Loss      : {stock['stop_loss']}",
        f"     Target 1       : {stock['target1']}",
        f"     Target 2       : {stock['target2']}",
        f"     Risk:Reward    : 1:{stock['rr_ratio']}",
    ])
    return "\n".join(lines)
```

---

## Edit 4: Replace the output sections in `format_results()` with unified display

Find this block (everything from the breakout sector section through MUST TRADE):

```python
    if results["breakout_stocks"]:
        lines.append("\n🚀 BREAKOUT SECTOR STOCKS (Top 3/sector)")
        ...
```

…all the way down through the existing MUST TRADE section.

**Replace ALL of it** with this single unified rendering block:

```python
    # ─────────────────────────────────────────────
    # UNIFIED DISPLAY: dedup stocks, segregate by F&O / Investment,
    # show all matched criteria as badges
    # ─────────────────────────────────────────────
    def _build_badges(stock):
        """Return list of badge strings based on what criteria the stock matched."""
        badges = []
        if stock.get("gfs"):                  badges.append("🎯 GFS")
        if stock.get("agfs"):                 badges.append("⚡ AGFS")
        if stock.get("sma_crossover"):        badges.append("📊 SMA Crossover")
        # Sector Breakout: the stock appears in results["breakout_stocks"]
        if stock["ticker"] in breakout_set:   badges.append("🚀 Sector Breakout")
        # Must Trade: combined criterion
        if stock["ticker"] in must_trade_set: badges.append("🔴 MUST TRADE")
        return badges

    # Build the index sets
    breakout_set = {
        s["ticker"]
        for stocks in results["breakout_stocks"].values()
        for s in stocks
    }
    must_trade_set = (
        {s["ticker"] for s in results["must_trade_gfs"]}
        | {s["ticker"] for s in results["must_trade_agfs"]}
    )

    # Collect ALL unique qualifying stocks
    all_stocks = {}  # ticker -> stock dict (with highest score variant)
    sources = (
        list(results.get("gfs_stocks", [])) +
        list(results.get("agfs_stocks", []))
    )
    for stocks_in_sector in results["breakout_stocks"].values():
        sources.extend(stocks_in_sector)

    for s in sources:
        t = s["ticker"]
        if t not in all_stocks:
            all_stocks[t] = s
        else:
            # Keep the variant with the highest score (they should match anyway)
            try:
                curr = int(all_stocks[t]["breakout_score"].split("/")[0])
                new  = int(s["breakout_score"].split("/")[0])
                if new > curr:
                    all_stocks[t] = s
            except Exception:
                pass

    # Split by F&O vs Investment
    fno_stocks = [s for s in all_stocks.values() if s.get("is_fno", True)]
    inv_stocks = [s for s in all_stocks.values() if not s.get("is_fno", True)]

    # Sort each by breakout score descending, then ticker
    def _score(s):
        try: return int(s["breakout_score"].split("/")[0])
        except: return 0
    fno_stocks.sort(key=lambda s: (-_score(s), s["ticker"]))
    inv_stocks.sort(key=lambda s: (-_score(s), s["ticker"]))

    # Render TRADING (F&O)
    lines.append("\n\n" + "=" * 60)
    lines.append(f"⚡ TRADING CANDIDATES (F&O — leveraged + short-sellable) — {len(fno_stocks)}")
    lines.append("=" * 60)
    if fno_stocks:
        for s in fno_stocks:
            lines.append(format_stock_block(s, criteria_list=_build_badges(s)))
            lines.append("")  # spacing
    else:
        lines.append("\n   No qualifying F&O stocks today.")

    # Render INVESTMENT (non-F&O)
    lines.append("\n\n" + "=" * 60)
    lines.append(f"📈 INVESTMENT CANDIDATES (NIFTY 500 ex-F&O — long-only) — {len(inv_stocks)}")
    lines.append("=" * 60)
    if inv_stocks:
        for s in inv_stocks:
            lines.append(format_stock_block(s, criteria_list=_build_badges(s)))
            lines.append("")
    else:
        lines.append("\n   No qualifying investment stocks today.")
```

---

## Edit 5: Update `save_scan_result` call site to pass new fields (optional)

If you want the SMA values persisted to `scan_results` for backtesting later, the easiest way is to skip schema changes for now — they're already in the `stock_data` dict, just not persisted. The existing `save_scan_result()` only writes the columns that exist in the DB table. New fields are silently dropped — no error.

**No action needed unless you want SMA values in the DB.** If you do, add columns to `scan_results` and extend the INSERT in `db.py`. Recommend skipping for now.

---

## Verification after applying

```bash
python market_scanner.py
```

Expected output format:

```
============================================================
⚡ TRADING CANDIDATES (F&O — leveraged + short-sellable) — 14
============================================================

  📌 NMDC @ ₹96.04 | ⚡ AGFS | 📊 SMA Crossover | 🚀 Sector Breakout | 🔴 MUST TRADE
     Breakout Score : 6/8
     RSI (D/W/M)    : 65.20 / 68.13 / 62.45
     RSI-SMA(14)    : 60.10 / 65.30 / 58.20
     SMA 21 Cross   : ₹93.18  (Δ +2.86 / +3.07%)
     Entry Type     : Breakout
     Entry Zone     : ₹95.5 – ₹96.5
     ...

  📌 RELIANCE @ ₹2,847.55 | 🎯 GFS
     ...
```

Stocks appear once with all matched criteria as badges. No more redundant GFS/AGFS/Must Trade sections.
