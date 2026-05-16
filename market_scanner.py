"""
NSE F&O Market Scanner — Phase B (DB-backed)
─────────────────────────────────────────────
Daily flow:
  1. Update corporate actions
  2. Incremental yfinance fetch (only days since last DB entry)
  3. Run all scans reading from Postgres
  4. Persist scan_results
  5. Write dated text report
"""

import time
import random
import warnings
warnings.filterwarnings('ignore')

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from db import (
    test_connection,
    init_schema,
    get_all_stocks,
    get_stocks_by_sector,
    get_latest_trade_date,
    insert_daily_prices,
    fetch_prices_df,
    get_latest_index_date,
    insert_index_prices,
    fetch_index_df,
    mark_data_refreshed,
    save_scan_result,
    start_job_run,
    finish_job_run,
)
from corporate_actions import (
    update_corporate_actions,
    get_quarantined_symbols,
    get_flagged_symbols,
    get_upcoming_actions,
)

# Sector → index mapping & stock universe (used during incremental fetch)
NSE_SECTOR_INDICES = {
    "Nifty Bank":               "^NSEBANK",
    "Nifty Private Bank":       "NIFTY_PVT_BANK.NS",
    "Nifty PSU Bank":           "^CNXPSUBANK",
    "Nifty Financial Services": "NIFTY_FIN_SERVICE.NS",
    "Nifty IT":                 "^CNXIT",
    "Nifty Pharma":             "^CNXPHARMA",
    "Nifty Healthcare":         "NIFTY_HEALTHCARE.NS",
    "Nifty Auto":               "^CNXAUTO",
    "Nifty FMCG":               "^CNXFMCG",
    "Nifty Metal":              "^CNXMETAL",
    "Nifty Realty":             "^CNXREALTY",
    "Nifty Media":              "^CNXMEDIA",
    "Nifty Chemicals":          "NIFTY_CHEM.NS",
    "Nifty Consumer Durables":  "NIFTY_CONSR_DURBL.NS",
    "Nifty Energy":             "^CNXENERGY",
    "Nifty Infra":              "^CNXINFRA",
    "Nifty Oil & Gas":          "NIFTY_OIL_AND_GAS.NS",
}
BROADER_INDICES = {"NIFTY 50": "^NSEI", "SENSEX": "^BSESN"}


# ─────────────────────────────────────────────
# INDICATORS
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
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_bollinger(series, period=20, std_dev=2):
    sma   = series.rolling(period).mean()
    std   = series.rolling(period).std()
    upper = sma + std_dev * std
    lower = sma - std_dev * std
    return upper, lower, (upper - lower) / sma


def compute_atr(df, period=14):
    high, low, close = df['High'], df['Low'], df['Close']
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()


def compute_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


# ─────────────────────────────────────────────
# PATTERNS (unchanged from v4)
# ─────────────────────────────────────────────
def detect_cup_and_handle(df, min_cup_bars=20, max_cup_bars=60):
    try:
        closes = df['Close'].values
        n = len(closes)
        if n < max_cup_bars + 10:
            return False
        window = closes[-(max_cup_bars + 15):]
        left_rim_idx = np.argmax(window[:max_cup_bars])
        left_rim = window[left_rim_idx]
        cup_bottom = np.min(window[left_rim_idx:left_rim_idx + max_cup_bars])
        depth_pct  = (left_rim - cup_bottom) / left_rim
        if not (0.15 <= depth_pct <= 0.50):
            return False
        right_rim_region = window[left_rim_idx + min_cup_bars:]
        if len(right_rim_region) < 5:
            return False
        right_rim = np.max(right_rim_region[:10])
        if abs(right_rim - left_rim) / left_rim > 0.05:
            return False
        handle_region = window[-10:]
        handle_low    = np.min(handle_region)
        handle_depth  = (right_rim - handle_low) / right_rim
        if handle_depth > 0.15:
            return False
        return bool(closes[-1] >= left_rim * 0.98)
    except Exception:
        return False


def detect_elliott_wave3(df, lookback=60):
    try:
        closes  = df['Close'].values[-lookback:]
        volumes = df['Volume'].values[-lookback:]
        n = len(closes)
        if n < 30:
            return False
        w1_region = closes[:n//3]
        w1_high   = np.max(w1_region)
        w1_high_idx = np.argmax(w1_region)
        if w1_high <= closes[0]:
            return False
        w1_move = w1_high - closes[0]
        w2_region = closes[w1_high_idx:w1_high_idx + n//3]
        w2_low    = np.min(w2_region)
        retracement = (w1_high - w2_low) / w1_move
        if not (0.382 <= retracement <= 0.618):
            return False
        if closes[-1] <= w1_high:
            return False
        w1_vol_avg = np.mean(volumes[:w1_high_idx + 1])
        w3_vol_avg = np.mean(volumes[w1_high_idx + n//3:])
        return bool(w3_vol_avg >= w1_vol_avg * 1.2)
    except Exception:
        return False


def detect_bollinger_squeeze_breakout(df, period=20):
    try:
        _, _, bw = compute_bollinger(df['Close'], period)
        bw = bw.dropna()
        if len(bw) < 30:
            return False
        recent_bw    = bw.iloc[-5:]
        prior_bw     = bw.iloc[-20:-5]
        was_squeezed = prior_bw.min() == bw.iloc[-25:-5].min()
        is_expanding = recent_bw.iloc[-1] > recent_bw.iloc[0]
        price_above_mid = df['Close'].iloc[-1] > df['Close'].rolling(period).mean().iloc[-1]
        return bool(was_squeezed and is_expanding and price_above_mid)
    except Exception:
        return False


# ─────────────────────────────────────────────
# FIBONACCI / ENTRY
# ─────────────────────────────────────────────
def compute_fibonacci_levels(swing_low, swing_high):
    diff = swing_high - swing_low
    return {
        "0.0":   swing_high,
        "0.5":   swing_high - 0.5   * diff,
        "0.618": swing_high - 0.618 * diff,
        "0.786": swing_high - 0.786 * diff,
        "1.618": swing_high + 0.618 * diff,
    }


def determine_entry(df):
    current = float(df['Close'].iloc[-1])
    atr     = float(compute_atr(df).iloc[-1])
    swing_high = float(df['High'].iloc[-50:].max())
    swing_low  = float(df['Low'].iloc[-50:].min())
    fibs = compute_fibonacci_levels(swing_low, swing_high)

    near_breakout = current >= swing_high * 0.99
    near_50  = abs(current - fibs["0.5"])   / fibs["0.5"]   < 0.02
    near_618 = abs(current - fibs["0.618"]) / fibs["0.618"] < 0.02

    if near_50 or near_618:
        entry_type = "Fibonacci Retracement"
        entry_low  = round(fibs["0.618"], 2)
        entry_high = round(fibs["0.5"],   2)
        stop_loss  = round(fibs["0.786"], 2)
        target1    = round(fibs["0.0"],   2)
        target2    = round(fibs["1.618"], 2)
    elif near_breakout:
        entry_type = "Breakout"
        entry_low  = round(current, 2)
        entry_high = round(current * 1.005, 2)
        stop_loss  = round(current - atr, 2)
        target1    = round(current + 2 * atr, 2)
        target2    = round(current + 3 * atr, 2)
    else:
        entry_type = "Watch"
        entry_low  = round(swing_high * 0.99, 2)
        entry_high = round(swing_high * 1.005, 2)
        stop_loss  = round(swing_high - atr, 2)
        target1    = round(swing_high + 2 * atr, 2)
        target2    = round(swing_high + 3 * atr, 2)

    risk   = entry_high - stop_loss
    reward = target1   - entry_high
    rr     = round(reward / risk, 2) if risk > 0 else 0

    return {
        "entry_type": entry_type,
        "entry_zone": f"₹{entry_low} – ₹{entry_high}",
        "stop_loss":  f"₹{stop_loss}",
        "target1":    f"₹{target1}",
        "target2":    f"₹{target2}",
        "rr_ratio":   rr,
    }


# ─────────────────────────────────────────────
# INCREMENTAL FETCH (the new piece for Phase B)
# ─────────────────────────────────────────────
def incremental_fetch_stock(symbol):
    """
    Pulls missing days from yfinance and inserts into DB.
    Returns: rows_inserted or 0 if up-to-date.
    """
    latest = get_latest_trade_date(symbol)
    today  = datetime.now().date()

    if latest and (today - latest).days <= 0:
        return 0  # already up-to-date

    # Use 5y to be safe even if last load was long ago; on conflict it's idempotent
    period = "5y" if not latest or (today - latest).days > 30 else "1mo"

    try:
        df = yf.download(f"{symbol}.NS", period=period, interval="1d",
                         progress=False, auto_adjust=True)
    except Exception:
        return 0
    if df is None or df.empty:
        return 0
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

    if latest:
        df = df[df.index.date > latest]
        if df.empty:
            return 0

    rows = insert_daily_prices(symbol, df)
    if rows:
        mark_data_refreshed(symbol)
    return rows


def incremental_fetch_index(name, ticker):
    latest = get_latest_index_date(name)
    today  = datetime.now().date()
    if latest and (today - latest).days <= 0:
        return 0
    period = "5y" if not latest or (today - latest).days > 30 else "1mo"
    try:
        df = yf.download(ticker, period=period, interval="1d",
                         progress=False, auto_adjust=True)
    except Exception:
        return 0
    if df is None or df.empty:
        return 0
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    if latest:
        df = df[df.index.date > latest]
        if df.empty:
            return 0
    return insert_index_prices(name, df)


# ─────────────────────────────────────────────
# DATA LOADING FROM DB
# ─────────────────────────────────────────────
def load_stock_data(symbol):
    """Returns (daily, weekly, monthly) DataFrames or (None, None, None)."""
    daily = fetch_prices_df(symbol)
    if daily is None or len(daily) < 60:
        return None, None, None

    # Resample to weekly and monthly (end-of-period)
    weekly = daily.resample("W").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna()
    monthly = daily.resample("ME").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna()

    if len(weekly) < 20 or len(monthly) < 14:
        return None, None, None
    return daily, weekly, monthly


# ─────────────────────────────────────────────
# CORE SCAN
# ─────────────────────────────────────────────
def scan_stock(symbol, flagged_set=None):
    daily, weekly, monthly = load_stock_data(symbol)
    if daily is None:
        return None

    close_d = daily['Close']
    rsi_daily   = float(compute_rsi(close_d).iloc[-1])
    rsi_weekly  = float(compute_rsi(weekly['Close']).iloc[-1])
    rsi_monthly = float(compute_rsi(monthly['Close']).iloc[-1])

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

    gfs  = (rsi_monthly > 60 and rsi_weekly > 60 and 40 <= rsi_daily <= 45)
    agfs = (rsi_monthly > 60 and rsi_weekly > 60 and rsi_daily > 60)

    entry = determine_entry(daily)
    if not ((score >= 4) or gfs or agfs):
        return None

    return {
        "ticker":         symbol,
        "current_price":  f"₹{round(current, 2)}",
        "breakout_score": f"{score}/7",
        "score_detail":   score_detail,
        "rsi_daily":      round(rsi_daily,   2),
        "rsi_weekly":     round(rsi_weekly,  2),
        "rsi_monthly":    round(rsi_monthly, 2),
        "gfs":            gfs,
        "agfs":           agfs,
        "corp_action_flag": bool(flagged_set and symbol in flagged_set),
        **entry,
    }


# ─────────────────────────────────────────────
# MARKET / SECTOR  (now from DB)
# ─────────────────────────────────────────────
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


def scan_sector_direction():
    out = {}
    for sector in NSE_SECTOR_INDICES.keys():
        df = fetch_index_df(sector)
        if df is None or len(df) < 30:
            out[sector] = {"status": "Data Unavailable", "rsi": 0}
            continue
        close   = df['Close']
        current = float(close.iloc[-1])
        ema20   = float(compute_ema(close, 20).iloc[-1])
        ema50   = float(compute_ema(close, 50).iloc[-1]) if len(close) >= 50 else ema20
        rsi     = float(compute_rsi(close).iloc[-1])
        _, _, hist = compute_macd(close)
        breakout = current > float(close.iloc[-50:].max() * 0.99) if len(close) >= 50 else False

        bull_points = sum([
            current > ema20,
            current > ema50,
            rsi > 55,
            float(hist.iloc[-1]) > 0,
            breakout,
        ])
        if bull_points >= 3:
            status = "Bullish Breakout"
        elif bull_points == 2:
            status = "Neutral"
        else:
            status = "Bearish"
        out[sector] = {"status": status, "rsi": round(rsi, 2)}
    return out


# ─────────────────────────────────────────────
# PIPELINE
# ─────────────────────────────────────────────
def run_full_scan():
    print("\n" + "="*60)
    print("  NSE F&O MARKET SCANNER (DB-backed)")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print("="*60)

    job_id = start_job_run("DAILY_SCAN")

    # [0] Corp actions
    print("\n[0/5] Updating Corporate Actions...")
    try:
        ca_result = update_corporate_actions()
    except Exception as e:
        print(f"   ⚠️  Corp action update failed: {e}")
        ca_result = {"new_risky": [], "risky_pending": []}

    flagged     = get_flagged_symbols()
    flagged_set = {f["symbol"] for f in flagged}
    quarantined = get_quarantined_symbols()
    upcoming_7d = get_upcoming_actions(days_ahead=7)

    # [1] Incremental fetch — indices
    print("\n[1/5] Incremental fetch: indices...")
    idx_updated = 0
    for name, ticker in {**NSE_SECTOR_INDICES, **BROADER_INDICES}.items():
        r = incremental_fetch_index(name, ticker)
        if r:
            print(f"   ✅ {name}: +{r} rows")
            idx_updated += r
        time.sleep(random.uniform(0.3, 0.7))
    print(f"   ✅ Indices: {idx_updated} new rows")

    # [2] Incremental fetch — stocks
    print("\n[2/5] Incremental fetch: stocks...")
    all_symbols = get_all_stocks(active_only=True)
    stock_updates = 0
    for sym in all_symbols:
        if sym in quarantined:
            continue
        r = incremental_fetch_stock(sym)
        if r:
            stock_updates += r
        time.sleep(random.uniform(0.2, 0.5))
    print(f"   ✅ Stocks: {stock_updates} new rows across {len(all_symbols)} symbols")

    # [3] Market & sectors
    print("\n[3/5] Scanning NIFTY/SENSEX + sectors...")
    market  = get_market_direction()
    sectors = scan_sector_direction()
    bullish_sectors = [s for s, v in sectors.items() if v.get("status") == "Bullish Breakout"]

    # [4] Stock scans
    print(f"\n[4/5] Scanning stocks in {len(bullish_sectors)} bullish sectors...")
    results = {
        "market":          market,
        "sectors":         sectors,
        "bullish_sectors": bullish_sectors,
        "breakout_stocks": {},
        "gfs_stocks":      [],
        "agfs_stocks":     [],
        "must_trade_gfs":  [],
        "must_trade_agfs": [],
        "corp_actions_flagged":     flagged,
        "corp_actions_upcoming_7d": upcoming_7d,
        "quarantined":     sorted(quarantined),
    }

    all_scanned = {}
    for sector in bullish_sectors:
        stocks = get_stocks_by_sector(sector)
        hits = []
        for sym in stocks:
            if sym in quarantined:
                continue
            if sym in all_scanned:
                if all_scanned[sym]:
                    hits.append(all_scanned[sym])
                continue
            r = scan_stock(sym, flagged_set)
            if r:
                all_scanned[sym] = {"sector": sector, **r}
                hits.append(r)
            else:
                all_scanned[sym] = None
        valid = [h for h in hits if h]
        valid.sort(key=lambda x: int(x["breakout_score"].split("/")[0]), reverse=True)
        if valid:
            results["breakout_stocks"][sector] = valid[:3]

    # GFS/AGFS scan across remaining symbols
    print("\n[5/5] Remaining GFS/AGFS scan...")
    for sym in all_symbols:
        if sym in all_scanned or sym in quarantined:
            continue
        r = scan_stock(sym, flagged_set)
        if r:
            # Find any sector membership (just for display)
            with __import__("contextlib").suppress(Exception):
                pass
            all_scanned[sym] = {"sector": "Other", **r}

    breakout_tickers = {
        s["ticker"] for stocks in results["breakout_stocks"].values() for s in stocks
    }

    seen_g, seen_a = set(), set()
    for sym, data in all_scanned.items():
        if not data:
            continue
        if data.get("gfs") and sym not in seen_g:
            seen_g.add(sym)
            results["gfs_stocks"].append(data)
            if sym in breakout_tickers:
                results["must_trade_gfs"].append(data)
        if data.get("agfs") and sym not in seen_a:
            seen_a.add(sym)
            results["agfs_stocks"].append(data)
            if sym in breakout_tickers:
                results["must_trade_agfs"].append(data)

    # Persist scan_results
    scan_date = datetime.now().date()
    must_trade_set = (
        {s["ticker"] for s in results["must_trade_gfs"]}
        | {s["ticker"] for s in results["must_trade_agfs"]}
    )
    saved = 0
    for sym, data in all_scanned.items():
        if not data:
            continue
        data["must_trade"] = sym in must_trade_set
        try:
            save_scan_result(scan_date, data)
            saved += 1
        except Exception as e:
            print(f"   ⚠️  save_scan_result {sym}: {e}")

    finish_job_run(job_id, "SUCCESS",
                   stocks_processed=len(all_symbols),
                   signals_found=saved)

    return results


# ─────────────────────────────────────────────
# FORMATTER
# ─────────────────────────────────────────────
def format_stock_block(stock):
    gfs_badge  = " | 🎯 GFS"  if stock.get("gfs")  else ""
    agfs_badge = " | ⚡ AGFS" if stock.get("agfs") else ""
    ca_warn    = " | 🚩 CORP ACTION" if stock.get("corp_action_flag") else ""

    return "\n".join([
        f"  📌 {stock['ticker']} @ {stock['current_price']}{gfs_badge}{agfs_badge}{ca_warn}",
        f"     Breakout Score : {stock['breakout_score']}",
        f"     RSI (D/W/M)    : {stock['rsi_daily']} / {stock['rsi_weekly']} / {stock['rsi_monthly']}",
        f"     Entry Type     : {stock['entry_type']}",
        f"     Entry Zone     : {stock['entry_zone']}",
        f"     Stop Loss      : {stock['stop_loss']}",
        f"     Target 1       : {stock['target1']}",
        f"     Target 2       : {stock['target2']}",
        f"     Risk:Reward    : 1:{stock['rr_ratio']}",
    ])


def format_results(results):
    lines = [
        "=" * 60,
        "📊 NSE F&O MARKET SCANNER REPORT",
        datetime.now().strftime("📅 %d %b %Y | ⏰ %I:%M %p"),
        "=" * 60,
    ]

    if results.get("corp_actions_flagged"):
        lines.append("\n🚩 CORPORATE ACTION FLAGS (review recommended)")
        lines.append("-" * 60)
        for f in results["corp_actions_flagged"]:
            lines.append(f"  {f['symbol']:<15} | {f['action_type']:<10} | ex-date: {f['ex_date']:<12}")
            if f.get("details"):
                lines.append(f"      {f['details'][:80]}")
        lines.append("-" * 60)

    if results.get("corp_actions_upcoming_7d"):
        lines.append("\n📅 UPCOMING CORP ACTIONS (next 7 days)")
        lines.append("-" * 60)
        for u in results["corp_actions_upcoming_7d"]:
            risky = " 🚩" if u.get("is_risky") else ""
            lines.append(f"  {u['ex_date']:<12} | {u['symbol']:<12} | {u['action_type']:<10}{risky}")
            if u.get("details"):
                lines.append(f"      {u['details'][:80]}")
        lines.append("-" * 60)

    if results.get("quarantined"):
        lines.append(f"\n⏸️  QUARANTINED: {', '.join(results['quarantined'])}")

    lines.append("\n🌐 MARKET DIRECTION")
    lines.append("-" * 30)
    for idx, data in results["market"].items():
        if isinstance(data, dict):
            lines.append(f"  {idx}: {data['direction']}  |  Price: {data['price']}  |  RSI: {data['rsi']}")
        else:
            lines.append(f"  {idx}: {data}")

    lines.append("\n📂 SECTOR STATUS")
    lines.append("-" * 30)
    for sector, data in results["sectors"].items():
        if isinstance(data, dict):
            status = data.get("status", "Unknown")
            emoji = {"Bullish Breakout": "🟢", "Neutral": "🟡", "Bearish": "🔴"}.get(status, "⚪")
            lines.append(f"  {emoji} {sector}: {status}  (RSI: {data.get('rsi', 'N/A')})")

    if results["breakout_stocks"]:
        lines.append("\n🚀 BREAKOUT SECTOR STOCKS (Top 3/sector)")
        lines.append("-" * 30)
        for sector, stocks in results["breakout_stocks"].items():
            lines.append(f"\n  [{sector}]")
            for stock in stocks:
                lines.append(format_stock_block(stock))
    else:
        lines.append("\n🚀 BREAKOUT STOCKS: None today")

    if results["gfs_stocks"]:
        lines.append("\n\n🎯 GFS STRATEGY PICKS")
        lines.append("-" * 30)
        for stock in results["gfs_stocks"]:
            lines.append(format_stock_block(stock))
    else:
        lines.append("\n\n🎯 GFS PICKS: None today")

    if results["agfs_stocks"]:
        lines.append("\n\n⚡ AGFS STRATEGY PICKS")
        lines.append("-" * 30)
        for stock in results["agfs_stocks"]:
            lines.append(format_stock_block(stock))
    else:
        lines.append("\n\n⚡ AGFS PICKS: None today")

    if results["must_trade_gfs"] or results["must_trade_agfs"]:
        lines.append("\n\n🔴 MUST TRADE (Breakout + Strategy Overlap)")
        lines.append("-" * 30)
        for stock in results["must_trade_gfs"]:
            lines.append(f"  🔴 [GFS] {stock['ticker']} — {stock['entry_zone']} | SL: {stock['stop_loss']} | T1: {stock['target1']} | RR: 1:{stock['rr_ratio']}")
        for stock in results["must_trade_agfs"]:
            lines.append(f"  🔴 [AGFS] {stock['ticker']} — {stock['entry_zone']} | SL: {stock['stop_loss']} | T1: {stock['target1']} | RR: 1:{stock['rr_ratio']}")
    else:
        lines.append("\n\n🔴 MUST TRADE: None today")

    lines.append("\n" + "=" * 60)
    lines.append("⚠️  Stocks marked 🚩 have recent corp actions; data may be unreliable")
    lines.append("⚠️  For informational purposes only. Trade responsibly.")
    lines.append("=" * 60)
    return "\n".join(lines)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    # Sanity-check DB
    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        raise SystemExit(1)

    results = run_full_scan()
    report  = format_results(results)
    print("\n\n")
    print(report)

    filename = f"scan_result_{datetime.now().strftime('%d_%b_%Y')}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n✅ Report saved to {filename}")
