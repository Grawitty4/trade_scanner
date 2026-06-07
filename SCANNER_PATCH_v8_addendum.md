# Addendum to SCANNER_PATCH_v8 — Criteria Met Line

This replaces **Edit 3** in SCANNER_PATCH_v8.md. Everything else stays the same.

The difference: `format_stock_block()` now also renders a "Criteria Met"
line below the score, listing the sub-criteria that contributed to the
score. Top-line badges remain unchanged (strategic outcomes only).

---

## Edit 3 (REVISED): Replace `format_stock_block()` with this version

Find `format_stock_block()` and replace with:

```python
def format_stock_block(stock, criteria_list=None):
    """
    Render a stock block. criteria_list = top-line badges (strategic outcomes).
    Sub-criteria (which mechanical conditions passed) are shown on a separate line.
    """
    seg_badge = "" if stock.get("is_fno", True) else " | 📈 INV"
    ca_warn   = " | 🚩 CORP ACTION" if stock.get("corp_action_flag") else ""

    badges = " | ".join(criteria_list) if criteria_list else ""
    badges_str = f" | {badges}" if badges else ""

    lines = [
        f"  📌 {stock['ticker']} @ {stock['current_price']}{badges_str}{ca_warn}{seg_badge}",
        f"     Breakout Score : {stock['breakout_score']}",
    ]

    # NEW: Criteria Met line — list the sub-criteria that passed
    # Pulls from the score_detail dict that scan_stock() populates
    score_detail = stock.get("score_detail", {})
    if score_detail:
        passed = [name for name, ok in score_detail.items() if ok]
        if passed:
            # Short, readable list
            lines.append(f"     Criteria Met   : {', '.join(passed)}")

    lines.append(
        f"     RSI (D/W/M)    : {stock['rsi_daily']} / {stock['rsi_weekly']} / {stock['rsi_monthly']}"
    )

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

## Expected output

```
============================================================
⚡ TRADING CANDIDATES (F&O — leveraged + short-sellable) — 14
============================================================

  📌 NMDC @ ₹96.04 | ⚡ AGFS | 📊 SMA Crossover | 🚀 Sector Breakout
     Breakout Score : 6/8
     Criteria Met   : Price≥Resistance, Volume≥1.5x, MACD Crossover, Cup & Handle, RSI>60 (Daily), SMA 21/63 Crossover
     RSI (D/W/M)    : 65.20 / 68.13 / 62.45
     RSI-SMA(14)    : 60.10 / 65.30 / 58.20
     SMA 21 Cross   : ₹93.18  (Δ +2.86 / +3.07%)
     Entry Type     : Breakout
     Entry Zone     : ₹95.50 – ₹96.50
     Stop Loss      : ₹93.20
     Target 1       : ₹98.80
     Target 2       : ₹101.40
     Risk:Reward    : 1:2.5
```

Now the 6/8 score and the 6 criteria listed are consistent.
