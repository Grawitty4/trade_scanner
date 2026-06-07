"""
Single-Stock Diagnostic — uses ADJUSTED prices.
─────────────────────────────────────────────────
Shows everything about a stock as our scanner sees it (post-adjustment):
  - Row count, date range
  - Latest close
  - Daily / Weekly / Monthly RSI (computed our way, on adjusted data)
  - Breakout score with each of the 7 criteria
  - GFS / AGFS qualification
  - Quality flag, corp action flags
  - Any active adjustment factors
  - Why the stock did or didn't qualify

Usage:
    python check_stock.py VEDL
    python check_stock.py VEDL HINDUNILVR RELIANCE
    python check_stock.py VEDL --raw     # show raw data instead of adjusted (debugging)
"""

import sys
import pandas as pd
import numpy as np
from datetime import datetime

from db import (
    test_connection,
    fetch_prices_df,
    fetch_prices_df_adjusted,
    get_cursor,
)


# ─────────────────────────────────────────────
# INDICATORS (mirror of market_scanner.py)
# ─────────────────────────────────────────────
def compute_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs  = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_macd(series, fast=12, slow=26, signal=9):
    ema_fast    = series.ewm(span=fast, adjust=False).mean()
    ema_slow    = series.ewm(span=slow, adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line


def compute_bollinger(series, period=20, std_dev=2):
    sma   = series.rolling(period).mean()
    std   = series.rolling(period).std()
    return sma + std_dev * std, sma - std_dev * std, ((sma + std_dev * std) - (sma - std_dev * std)) / sma


# ─────────────────────────────────────────────
# DIAGNOSTIC
# ─────────────────────────────────────────────
def diagnose(symbol, use_raw=False):
    print("\n" + "=" * 65)
    print(f"  DIAGNOSING: {symbol}  ({'RAW' if use_raw else 'ADJUSTED'})")
    print("=" * 65)

    # 1) Stock master info
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT yfinance_ticker, is_active, data_quality_flag, notes,
                   last_data_refresh
            FROM stocks WHERE symbol = %s
        """, (symbol,))
        row = cur.fetchone()
    if not row:
        print(f"❌ {symbol} is NOT in stocks table.")
        return
    yf_ticker, is_active, quality_flag, notes, last_refresh = row
    print(f"\n[Master Record]")
    print(f"   yfinance_ticker  : {yf_ticker}")
    print(f"   is_active        : {is_active}")
    print(f"   data_quality_flag: {quality_flag}")
    print(f"   last_data_refresh: {last_refresh}")
    if notes:
        print(f"   notes            : {notes}")

    # 2) Adjustment factors in effect
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT action_type, effective_date, price_factor, volume_factor, notes
            FROM corporate_action_adjustments
            WHERE symbol = %s
            ORDER BY effective_date
        """, (symbol,))
        adjustments = cur.fetchall()

    if adjustments:
        print(f"\n[Active Adjustments ({len(adjustments)})]")
        for a in adjustments:
            print(f"   • {a[0]:<10} | eff: {a[1]} | price_factor: {a[2]} | vol_factor: {a[3]}")
            if a[4]:
                print(f"     {a[4][:90]}")
    else:
        print(f"\n[Active Adjustments]   None")

    # 3) Price data (raw OR adjusted)
    if use_raw:
        df = fetch_prices_df(symbol)
    else:
        df = fetch_prices_df_adjusted(symbol)

    if df is None or df.empty:
        print(f"\n❌ No price data for {symbol}.")
        return

    print(f"\n[Price Data]")
    print(f"   Row count    : {len(df)}")
    print(f"   Date range   : {df.index.min().date()} → {df.index.max().date()}")
    print(f"   Latest close : ₹{df['Close'].iloc[-1]:.2f}")
    print(f"   Latest volume: {df['Volume'].iloc[-1]:,.0f}")
    print(f"   50-day high  : ₹{df['High'].iloc[-50:].max():.2f}")

    if len(df) < 60:
        print(f"\n⚠️  Insufficient data: requires ≥60 daily rows. Skipping further analysis.")
        return

    # 4) Resample to weekly/monthly
    weekly = df.resample("W").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna()
    monthly = df.resample("ME").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna()

    print(f"\n[Resampled Counts]")
    print(f"   Weekly rows  : {len(weekly)}  (need ≥20)")
    print(f"   Monthly rows : {len(monthly)} (need ≥14)")

    if len(weekly) < 20 or len(monthly) < 14:
        print(f"\n⚠️  Insufficient weekly/monthly history. Skipping.")
        return

    # 5) RSI values
    rsi_d = float(compute_rsi(df['Close']).iloc[-1])
    rsi_w = float(compute_rsi(weekly['Close']).iloc[-1])
    rsi_m = float(compute_rsi(monthly['Close']).iloc[-1])

    print(f"\n[RSI (14-period, EWM)]")
    print(f"   Daily   : {rsi_d:.2f}")
    print(f"   Weekly  : {rsi_w:.2f}")
    print(f"   Monthly : {rsi_m:.2f}")

    # 6) Strategy qualification
    gfs  = (rsi_m > 60 and rsi_w > 60 and 40 <= rsi_d <= 45)
    agfs = (rsi_m > 60 and rsi_w > 60 and rsi_d > 60)

    print(f"\n[Strategy Match]")
    print(f"   GFS  rule: monthly>60 AND weekly>60 AND 40<=daily<=45")
    print(f"        actual: {rsi_m:.1f} > 60 = {rsi_m > 60} | "
          f"{rsi_w:.1f} > 60 = {rsi_w > 60} | "
          f"40 <= {rsi_d:.1f} <= 45 = {40 <= rsi_d <= 45}")
    print(f"        → GFS  : {'✅ MATCH' if gfs else '❌ NO MATCH'}")

    print(f"   AGFS rule: monthly>60 AND weekly>60 AND daily>60")
    print(f"        actual: {rsi_m:.1f} > 60 = {rsi_m > 60} | "
          f"{rsi_w:.1f} > 60 = {rsi_w > 60} | "
          f"{rsi_d:.1f} > 60 = {rsi_d > 60}")
    print(f"        → AGFS : {'✅ MATCH' if agfs else '❌ NO MATCH'}")

    # 7) Breakout score (7 criteria)
    print(f"\n[Breakout Score (7 criteria)]")
    score = 0

    swing_high = float(df['High'].iloc[-50:].max())
    current    = float(df['Close'].iloc[-1])
    c1 = current >= swing_high * 0.99
    score += int(c1)
    print(f"   1. Price ≥ 99% of 50d swing high: ₹{current:.2f} vs ₹{swing_high:.2f} → {'✅' if c1 else '❌'}")

    vol_today = float(df['Volume'].iloc[-1])
    vol_avg20 = float(df['Volume'].iloc[-21:-1].mean())
    c2 = vol_today >= vol_avg20 * 1.5
    score += int(c2)
    print(f"   2. Volume ≥ 1.5× 20d avg: {vol_today:,.0f} vs {vol_avg20*1.5:,.0f} → {'✅' if c2 else '❌'}")

    _, _, bw = compute_bollinger(df['Close'])
    bw = bw.dropna()
    c3 = False
    if len(bw) >= 25:
        recent_bw  = bw.iloc[-5:]
        prior_bw   = bw.iloc[-20:-5]
        was_squeezed = prior_bw.min() == bw.iloc[-25:-5].min()
        is_expanding = recent_bw.iloc[-1] > recent_bw.iloc[0]
        c3 = bool(was_squeezed and is_expanding and current > df['Close'].rolling(20).mean().iloc[-1])
    score += int(c3)
    print(f"   3. BB Squeeze → Expansion: {'✅' if c3 else '❌'}")

    _, _, hist = compute_macd(df['Close'])
    c4 = bool(hist.iloc[-1] > 0 and hist.iloc[-2] <= 0)
    score += int(c4)
    print(f"   4. MACD bullish crossover: hist[-1]={hist.iloc[-1]:.2f} hist[-2]={hist.iloc[-2]:.2f} → {'✅' if c4 else '❌'}")

    print(f"   5. Cup & Handle:           (not computed in this diagnostic)")
    print(f"   6. Elliott Wave 3:         (not computed in this diagnostic)")

    c7 = rsi_d > 60
    score += int(c7)
    print(f"   7. RSI > 60 (Daily): {rsi_d:.1f} > 60 → {'✅' if c7 else '❌'}")

    print(f"\n   Breakout score so far: {score}/7 (criteria 5 & 6 not shown — would add 0-2)")

    # 8) Final qualification
    qualifies = (score >= 4) or gfs or agfs
    print(f"\n[Final Qualification]")
    print(f"   Qualifies for scanner output: {'✅ YES' if qualifies else '❌ NO'}")
    print(f"      Breakout ≥4/7 : {score >= 4}")
    print(f"      GFS           : {gfs}")
    print(f"      AGFS          : {agfs}")

    # 9) Corp action info
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT action_type, raw_text, ex_date, is_risky,
                   user_decision, quarantine_until
            FROM corporate_actions WHERE symbol = %s
            ORDER BY discovered_at DESC LIMIT 5
        """, (symbol,))
        actions = cur.fetchall()

    if actions:
        print(f"\n[Recent Corp Actions ({len(actions)})]")
        for a in actions:
            print(f"   • {a[0]:<10} | ex-date: {a[2]:<12} | risky: {a[3]} | "
                  f"decision: {a[4]} | quarantine_until: {a[5]}")
            if a[1]:
                print(f"     details: {a[1][:80]}")


def main():
    args = sys.argv[1:]
    use_raw = "--raw" in args
    symbols = [a.upper() for a in args if not a.startswith("--")]
    if not symbols:
        print("Usage: python check_stock.py SYMBOL [SYMBOL2 ...] [--raw]")
        sys.exit(0)

    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return

    for sym in symbols:
        diagnose(sym, use_raw=use_raw)


if __name__ == "__main__":
    main()
