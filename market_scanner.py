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
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    """Return current datetime in IST (Indian Standard Time)."""
    return datetime.now(IST)


from db import (
    test_connection,
    init_schema,
    get_all_stocks,
    get_stocks_by_sector,
    get_latest_trade_date,
    get_latest_trade_dates_bulk,
    get_quality_flagged_symbols,
    insert_daily_prices,
    fetch_prices_df,
    fetch_prices_df_adjusted,
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
    "Nifty Healthcare":         "^CNXHC",
    "Nifty Auto":               "^CNXAUTO",
    "Nifty FMCG":               "^CNXFMCG",
    "Nifty Metal":              "^CNXMETAL",
    "Nifty Realty":             "^CNXREALTY",
    "Nifty Media":              "^CNXMEDIA",
    "Nifty Chemicals":          "^CNXCHEM",
    "Nifty Consumer Durables":  "^CNXCONSDUR",
    "Nifty Energy":             "^CNXENERGY",
    "Nifty Infra":              "^CNXINFRA",
    "Nifty Oil & Gas":          "^CNXOILGAS",
    "Nifty India Defence":      "NIFTY_IND_DEFENCE.NS",
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

def detect_negative_divergence(df, rsi_series, lookback=21):
    """
    Detect Negative Divergence (ND) on the given timeframe:
      • In the last `lookback` bars, identify the two highest PRICE peaks
        (chronologically). If the later peak has HIGHER price than the earlier one,
        that's a "higher high" in price.
      • Check the RSI values AT those same two peak dates. If RSI at the later
        peak is LOWER than at the earlier peak, that's a "lower high" in RSI.
      • Both conditions together = Negative Divergence is OBSERVED.
      • If additionally today's close < yesterday's close → ND is ACTIVE.

    Returns dict:
      - status: 'none' | 'observed' | 'active'
      - peak1_date, peak1_price, peak1_rsi
      - peak2_date, peak2_price, peak2_rsi
    """
    import numpy as np
    out = {
        "status": "none",
        "peak1_date": None, "peak1_price": None, "peak1_rsi": None,
        "peak2_date": None, "peak2_price": None, "peak2_rsi": None,
    }
    if df is None or len(df) < lookback + 2 or rsi_series is None:
        return out

    window = df.iloc[-lookback:]
    rsi_window = rsi_series.iloc[-lookback:]
    if rsi_window.dropna().empty:
        return out

    highs = window['High'].values
    dates = window.index

    # Find top-2 highs in the window. Require at least 3 bars apart so we
    # don't pick the same "peak" twice.
    sorted_idx_by_high = sorted(range(len(highs)), key=lambda i: highs[i], reverse=True)
    if len(sorted_idx_by_high) < 2:
        return out

    top1 = sorted_idx_by_high[0]
    top2 = None
    for cand in sorted_idx_by_high[1:]:
        if abs(cand - top1) >= 3:  # min separation
            top2 = cand
            break
    if top2 is None:
        return out

    # Order chronologically: earlier index first
    a, b = (top1, top2) if top1 < top2 else (top2, top1)
    p1, p2 = float(highs[a]), float(highs[b])
    r1 = float(rsi_window.iloc[a]) if not np.isnan(rsi_window.iloc[a]) else None
    r2 = float(rsi_window.iloc[b]) if not np.isnan(rsi_window.iloc[b]) else None

    if r1 is None or r2 is None:
        return out

    out["peak1_date"]  = dates[a].date()
    out["peak1_price"] = round(p1, 2)
    out["peak1_rsi"]   = round(r1, 2)
    out["peak2_date"]  = dates[b].date()
    out["peak2_price"] = round(p2, 2)
    out["peak2_rsi"]   = round(r2, 2)

    # Higher high in price, lower high in RSI → ND observed
    if p2 > p1 and r2 < r1:
        out["status"] = "observed"
        if len(df) >= 2:
            today_close = float(df['Close'].iloc[-1])
            prev_low    = float(df['Low'].iloc[-2])
            if today_close < prev_low:
                out["status"] = "active"

    return out

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

def _try_elliott_at_lookback(df, lookback):
    """
    Single-attempt Elliott labeler at a given lookback.
    Returns (current_phase, phases_list, validation_failed_bool, violations_list).
    """
    import numpy as np
    if df is None or len(df) < lookback:
        return "?", [], True, ["insufficient_data"]

    closes = df['Close'].iloc[-lookback:].values
    dates  = df.index[-lookback:]

    # Zigzag pivots with 5% threshold
    pivots = []
    min_move = 0.05
    last_idx = 0
    last_price = closes[0]
    direction = None

    for i in range(1, len(closes)):
        change = (closes[i] - last_price) / last_price
        if direction is None:
            if abs(change) >= min_move:
                direction = "up" if change > 0 else "down"
                pivots.append((last_idx, last_price,
                               "L" if direction == "up" else "H"))
        elif direction == "up":
            if closes[i] > last_price:
                last_idx, last_price = i, closes[i]
            elif (last_price - closes[i]) / last_price >= min_move:
                pivots.append((last_idx, last_price, "H"))
                direction = "down"
                last_idx, last_price = i, closes[i]
        else:
            if closes[i] < last_price:
                last_idx, last_price = i, closes[i]
            elif (closes[i] - last_price) / last_price >= min_move:
                pivots.append((last_idx, last_price, "L"))
                direction = "up"
                last_idx, last_price = i, closes[i]

    pivots.append((len(closes) - 1, closes[-1],
                   "H" if direction == "up" else "L" if direction == "down" else "?"))

    if len(pivots) < 3:
        return "?", [], True, ["insufficient_pivots"]

    recent = pivots[-8:] if len(pivots) >= 8 else pivots

    # Trim to start at a Low (impulse motive starts at a trough)
    start = 0
    for i, p in enumerate(recent):
        if p[2] == "L":
            start = i
            break
    seq = recent[start:]

    if len(seq) < 3:
        return "?", [], True, ["too_few_pivots_after_trim"]

    labels = ["1", "2", "3", "4", "5", "A", "B", "C"][:len(seq) - 1]

    phases = []
    for i, label in enumerate(labels):
        s = seq[i]
        e = seq[i + 1]
        phases.append({
            "label":       label,
            "start_date":  dates[s[0]].date(),
            "start_price": float(s[1]),
            "end_date":    dates[e[0]].date(),
            "end_price":   float(e[1]),
        })

    # Rule validation
    violations = []
    if len(phases) >= 2:
        if phases[1]["end_price"] <= phases[0]["start_price"]:
            violations.append("W2 retraced >=100% of W1")
    if len(phases) >= 5:
        w1_len = abs(phases[0]["end_price"] - phases[0]["start_price"])
        w3_len = abs(phases[2]["end_price"] - phases[2]["start_price"])
        w5_len = abs(phases[4]["end_price"] - phases[4]["start_price"])
        if w3_len < w1_len and w3_len < w5_len:
            violations.append("W3 is shortest of impulse waves")
    if len(phases) >= 4:
        w1_high = max(phases[0]["start_price"], phases[0]["end_price"])
        w4_low  = min(phases[3]["start_price"], phases[3]["end_price"])
        if w4_low <= w1_high:
            violations.append("W4 overlapped W1 territory")

    if violations:
        return "?", phases, True, violations

    return labels[-1] if labels else "?", phases, False, []


def label_elliott_phase(df):
    """
    Try cascading lookbacks (120, 250, 500, 1000 daily bars) until a valid
    Elliott sequence is found. Returns:
      - current_phase: '1'|'2'|'3'|'4'|'5'|'A'|'B'|'C'|'?'
      - degree: 'short-term'|'medium-term'|'long-term'|'very-long-term'|None
      - lookback_used: int (days) or None
      - phases: list of phase dicts
      - validation_failed: bool
      - violations: list of violation strings (only if all lookbacks failed)
    """
    lookback_tiers = [
        (120,  "short-term"),       # ~6 months
        (250,  "medium-term"),      # ~1 year
        (500,  "long-term"),        # ~2 years
        (1000, "very-long-term"),   # ~4 years
    ]

    last_attempt = None  # remember most-recent attempt for graceful fallback

    for lookback, degree in lookback_tiers:
        if df is None or len(df) < lookback:
            continue
        phase, phases, failed, violations = _try_elliott_at_lookback(df, lookback)
        last_attempt = (phase, phases, failed, violations, lookback, degree)
        if not failed:
            return {
                "current_phase":     phase,
                "degree":            degree,
                "lookback_used":     lookback,
                "phases":            phases,
                "validation_failed": False,
                "violations":        [],
            }

    if last_attempt is None:
        return {
            "current_phase":     "?",
            "degree":            None,
            "lookback_used":     None,
            "phases":            [],
            "validation_failed": True,
            "violations":        ["insufficient_history_for_any_lookback"],
        }

    phase, phases, failed, violations, lookback, degree = last_attempt
    return {
        "current_phase":     "?",
        "degree":            degree,
        "lookback_used":     lookback,
        "phases":            phases,
        "validation_failed": True,
        "violations":        violations,
    }


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
def get_fno_flag_map():
    """Returns {symbol: is_fno_bool} for all active stocks."""
    from db import get_cursor
    with get_cursor() as (_, cur):
        cur.execute("SELECT symbol, is_fno FROM stocks WHERE is_active = TRUE")
        return {r[0]: bool(r[1]) for r in cur.fetchall()}

def get_sectors_for_symbol(symbol):
    """
    Return a comma-separated string of sectors this symbol belongs to.
    Returns 'Other' if no sector mapping found.
    """
    from db import get_cursor
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT sector FROM stock_sectors
            WHERE symbol = %s
            ORDER BY sector
        """, (symbol,))
        rows = [r[0] for r in cur.fetchall()]
    return ", ".join(rows) if rows else "Other"

def incremental_fetch_stock(symbol):
    """
    Pulls missing days from yfinance and inserts into DB.
    Returns: rows_inserted or 0 if up-to-date.
    """
    latest = get_latest_trade_date(symbol)
    today  = datetime.now().date()

    if latest and latest>today:
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
        df = df[df.index.date >= latest]
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
    daily = fetch_prices_df_adjusted(symbol)
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
def _is_skipped_by_quality_flag(symbol):
    """Returns reason string if stock should be skipped, else None."""
    from db import get_cursor
    with get_cursor() as (_, cur):
        cur.execute("SELECT data_quality_flag FROM stocks WHERE symbol = %s",
                    (symbol,))
        row = cur.fetchone()
    if row and row[0] and row[0] != 'OK':
        return row[0]
    return None


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

    # RSI vs RSI-SMA zones for each timeframe
    zone_d = rsi_sma_zone(rsi_daily,   rsi_d_sma_val)
    zone_w = rsi_sma_zone(rsi_weekly,  rsi_w_sma_val)
    zone_m = rsi_sma_zone(rsi_monthly, rsi_m_sma_val)
    all_three_green = (zone_d == "green" and zone_w == "green" and zone_m == "green")

    # Negative divergence detection (on daily)
    nd_daily = detect_negative_divergence(daily, rsi_d_series, lookback=21)

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

    # 9th criterion: RSI above its 14-period SMA on ALL three timeframes (strict)
    c9 = all_three_green
    score += int(c9); score_detail["RSI > RSI-SMA (D+W+M)"] = c9

    gfs  = (rsi_monthly > 60 and rsi_weekly > 60 and 40 <= rsi_daily <= 45)
    agfs = (rsi_monthly > 60 and rsi_weekly > 60 and 60 <= rsi_daily <= 65)

    entry = determine_entry(daily)
    elliott = label_elliott_phase(daily)

    # Qualifies if any of these are true (broadened to include SMA crossover by itself)
    if not ((score >= 4) or gfs or agfs or sma_cross["crossover"]):
        return None

    return {
        "ticker":         symbol,
        "current_price":  f"₹{round(current, 2)}",
        "current_price_raw": round(current, 2),
        "breakout_score": f"{score}/9",
        "score_detail":   score_detail,
        "rsi_daily":      round(rsi_daily,   2),
        "rsi_weekly":     round(rsi_weekly,  2),
        "rsi_monthly":    round(rsi_monthly, 2),
        "rsi_daily_sma":   round(rsi_d_sma_val, 2)   if rsi_d_sma_val is not None else None,
        "rsi_weekly_sma":  round(rsi_w_sma_val, 2)   if rsi_w_sma_val is not None else None,
        "rsi_monthly_sma": round(rsi_m_sma_val, 2)   if rsi_m_sma_val is not None else None,
        "rsi_daily_zone":   zone_d,
        "rsi_weekly_zone":  zone_w,
        "rsi_monthly_zone": zone_m,
        "sma_crossover": sma_cross["crossover"],
        "sma_intersection_price": round(sma_cross["intersection_price"], 2)
            if sma_cross["intersection_price"] is not None else None,
        "sma_diff_abs": sma_cross["diff_abs"],
        "sma_diff_pct": sma_cross["diff_pct"],
        "gfs":  gfs,
        "agfs": agfs,
        "corp_action_flag": bool(flagged_set and symbol in flagged_set),
        "nd_status":       nd_daily["status"],
        "nd_peak1_date":   nd_daily.get("peak1_date"),
        "nd_peak1_price": nd_daily.get("peak1_price"),
        "nd_peak1_rsi":    nd_daily.get("peak1_rsi"),
        "nd_peak2_date":   nd_daily.get("peak2_date"),
        "nd_peak2_price": nd_daily.get("peak2_price"),
        "nd_peak2_rsi":    nd_daily.get("peak2_rsi"),
        "elliott_phase":    elliott["current_phase"],
        "elliott_degree":   elliott.get("degree"),
        "elliott_lookback": elliott.get("lookback_used"),
        "elliott_phases":   elliott["phases"],
        "elliott_validation_failed": elliott.get("validation_failed", False),
        "elliott_violations":       elliott.get("violations", []),
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
        rsi_series = compute_rsi(close)
        rsi = float(rsi_series.iloc[-1])
        market_nd = detect_negative_divergence(df, rsi_series, lookback=21)
        macd_l, sig_l, _ = compute_macd(close)
        macd_bull = float(macd_l.iloc[-1]) > float(sig_l.iloc[-1])

        # Week-on-week and month-on-month % changes
        # We approximate: 5 trading days ≈ 1 week, 21 trading days ≈ 1 month
        def _pct_change(idx_back):
            if len(close) <= idx_back:
                return None
            past = float(close.iloc[-(idx_back + 1)])
            return ((current - past) / past) * 100 if past else None

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
            "nd_status":       market_nd["status"],
            "nd_peak1_date":   market_nd.get("peak1_date"),
            "nd_peak1_price":  market_nd.get("peak1_price"),
            "nd_peak1_rsi":    market_nd.get("peak1_rsi"),
            "nd_peak2_date":   market_nd.get("peak2_date"),
            "nd_peak2_price":  market_nd.get("peak2_price"),
            "nd_peak2_rsi":    market_nd.get("peak2_rsi"),
        }
    return results


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
            "Close": "last",
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

        # ND on weekly for sector context (use weekly RSI series + price)
        sector_nd = detect_negative_divergence(weekly, rsi_weekly, lookback=21)

        out[sector] = {
            "status":         status,
            "rsi":            round(rsi_this_week, 2),    # backward-compat
            "rsi_last_week":  round(rsi_last_week, 2) if rsi_last_week is not None else None,
            "rsi_this_week":  round(rsi_this_week, 2),
            "source":         source,
            "nd_status":       sector_nd["status"],
            "nd_peak1_date":   sector_nd.get("peak1_date"),
            "nd_peak1_price":  sector_nd.get("peak1_price"),
            "nd_peak1_rsi":    sector_nd.get("peak1_rsi"),
            "nd_peak2_date":   sector_nd.get("peak2_date"),
            "nd_peak2_price":  sector_nd.get("peak2_price"),
            "nd_peak2_rsi":    sector_nd.get("peak2_rsi"),
        }
    return out


# ─────────────────────────────────────────────
# PIPELINE
# ─────────────────────────────────────────────
def run_full_scan():
    print("\n" + "="*60)
    print("  NSE F&O MARKET SCANNER (DB-backed)")
    import time as _time
    _t0 = _time.time()
    _stage_times = {}
    print(f"  {now_ist().strftime('%d %b %Y, %I:%M %p IST')}")
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
    upcoming_14d = get_upcoming_actions(days_ahead=14)
    _stage_times["corp_actions"] = _time.time() - _t0

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
    _stage_times["incremental_indices"] = _time.time() - _t0

    # [2] Incremental fetch — stocks
    # [2] Incremental fetch — stocks (only fetch when behind)
    print("\n[2/5] Incremental fetch: stocks...")
    all_symbols = get_all_stocks(active_only=True)
    fno_map = get_fno_flag_map()

    # Skip stocks flagged with quality issues (e.g., LTM with insufficient history)
    flagged_bad = get_quality_flagged_symbols()
    if flagged_bad:
        print(f"   ⏭️  Skipping {len(flagged_bad)} flagged stocks: "
              f"{', '.join(sorted(flagged_bad))}")

    # Bulk lookup of latest dates — one query instead of 200
    today = datetime.now().date()
    latest_dates = get_latest_trade_dates_bulk(all_symbols)

    needs_update = []
    for sym in all_symbols:
        if sym in quarantined or sym in flagged_bad:
            continue
        latest = latest_dates.get(sym)
        if latest is None or latest<=today:
            needs_update.append(sym)

    print(f"   ℹ️  {len(all_symbols) - len(needs_update)} up-to-date, "
          f"{len(needs_update)} need fetch")

    stock_updates = 0
    if needs_update:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(incremental_fetch_stock, s): s for s in needs_update}
            for f in as_completed(futures):
                try:
                    r = f.result()
                    if r:
                        stock_updates += r
                except Exception as e:
                    print(f"   ⚠️  Fetch error for {futures[f]}: {e}")
    print(f"   ✅ Stocks: {stock_updates} new rows")
    _stage_times["incremental_stocks"] = _time.time() - _t0

    # [3] Market & sectors
    print("\n[3/5] Scanning NIFTY/SENSEX + sectors...")
    market  = get_market_direction()
    sectors = scan_sector_direction()
    bullish_sectors = [s for s, v in sectors.items() if v.get("status") == "Bullish Breakout"]
    _stage_times["market_sectors"] = _time.time() - _t0

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
        "corp_actions_upcoming_14d": upcoming_14d,
        "quarantined":     sorted(quarantined),
    }

    all_scanned = {}
    # Build the set of all unique symbols to scan in bullish sectors (dedup across overlapping sectors)
    sector_to_stocks = {}
    unique_to_scan = set()
    for sector in bullish_sectors:
        stocks = get_stocks_by_sector(sector)
        sector_to_stocks[sector] = stocks
        for sym in stocks:
            if sym in quarantined or sym in flagged_bad:
                continue
            unique_to_scan.add(sym)

    # Parallel scan
    if unique_to_scan:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(scan_stock, s, flagged_set): s for s in unique_to_scan}
            for f in as_completed(futures):
                sym = futures[f]
                try:
                    r = f.result()
                    all_scanned[sym] = r
                    if r:
                        all_scanned[sym]["is_fno"] = fno_map.get(sym, True)
                except Exception as e:
                    print(f"   ⚠️  Scan error for {sym}: {e}")
                    all_scanned[sym] = None

    # Now build the per-sector top-3 lists from the scanned results
    for sector in bullish_sectors:
        hits = []
        for sym in sector_to_stocks.get(sector, []):
            r = all_scanned.get(sym)
            if r and not (sym in quarantined or sym in flagged_bad):
                # Attach the sector this hit came from (a stock can be in multiple sectors,
                # but we display it under whichever sector first led to its inclusion)
                if "sector" not in r:
                    r = {"sector": sector, **r}
                    all_scanned[sym] = r
                    if r:
                        all_scanned[sym]["is_fno"] = fno_map.get(sym, True)
                hits.append(r)
        hits.sort(key=lambda x: int(x["breakout_score"].split("/")[0]), reverse=True)
        if hits:
            results["breakout_stocks"][sector] = hits[:3]
    _stage_times["stock_scan_loop"] = _time.time() - _t0

    # GFS/AGFS scan across remaining symbols
    print("\n[5/5] Remaining GFS/AGFS scan...")
    remaining = [s for s in all_symbols
                 if s not in all_scanned and s not in quarantined and s not in flagged_bad]

    if remaining:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(scan_stock, s, flagged_set): s for s in remaining}
            for f in as_completed(futures):
                sym = futures[f]
                try:
                    r = f.result()
                    if r:
                        all_scanned[sym] = {"sector": get_sectors_for_symbol(sym), **r, "is_fno": fno_map.get(sym, True)}
                except Exception as e:
                    print(f"   ⚠️  Scan error for {sym}: {e}")
    _stage_times["gfsagfs_calc_loop"] = _time.time() - _t0

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

    total = _time.time() - _t0
    print("\n⏱  TIMING")
    last = 0
    for stage, t in _stage_times.items():
        elapsed = t - last
        print(f"   {stage:<25} {elapsed:>7.1f}s  (cumulative {t:>7.1f}s)")
        last = t
    print(f"   {'TOTAL':<25} {total:>7.1f}s")
    return results


# ─────────────────────────────────────────────
# FORMATTER
# ─────────────────────────────────────────────
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

    # SMA crossover info if present
    if stock.get("sma_crossover"):
        inter = stock.get("sma_intersection_price")
        diff_abs = stock.get("sma_diff_abs")
        diff_pct = stock.get("sma_diff_pct")
        sign_abs = f"{diff_abs:+.2f}" if diff_abs is not None else "—"
        sign_pct = f"{diff_pct:+.2f}%" if diff_pct is not None else "—"
        lines.append(f"     SMA 21 Cross   : ₹{inter}  (Δ {sign_abs} / {sign_pct})")

    # Elliott Wave (heuristic with rule validation + cascading lookback)
    elliott_phase      = stock.get("elliott_phase")
    elliott_degree     = stock.get("elliott_degree")
    elliott_phases     = stock.get("elliott_phases", [])
    elliott_failed     = stock.get("elliott_validation_failed", False)
    elliott_violations = stock.get("elliott_violations", [])

    if elliott_phase and elliott_phase != "?":
        degree_tag = f" [{elliott_degree}]" if elliott_degree else ""
        lines.append(f"     Elliott Wave   : currently in phase {elliott_phase}{degree_tag}")
        for p in elliott_phases:
            lines.append(
                f"        W{p['label']}: {p['start_date']} ₹{p['start_price']:.2f} "
                f"→ {p['end_date']} ₹{p['end_price']:.2f}"
            )
    elif elliott_failed:
        viol_str = ", ".join(elliott_violations[:2]) if elliott_violations else "no clean structure"
        lines.append(
            f"     Elliott Wave   : ? (no rule-compliant sequence; last tried "
            f"{elliott_degree or 'short-term'}: {viol_str})"
        )

    # Negative Divergence
    nd_status = stock.get("nd_status", "none")
    if nd_status != "none":
        emoji = "🔴" if nd_status == "active" else "🟡"
        label = "ND ACTIVE" if nd_status == "active" else "ND observed"
        p1d = stock.get("nd_peak1_date")
        p1p = stock.get("nd_peak1_price")
        p1r = stock.get("nd_peak1_rsi")
        p2d = stock.get("nd_peak2_date")
        p2p = stock.get("nd_peak2_price")
        p2r = stock.get("nd_peak2_rsi")
        lines.append(f"     {emoji} {label}: price ↑ but RSI ↓ in last 21 days")
        if p1d and p2d:
            lines.append(
                f"        Peak 1 ({p1d}): ₹{p1p} (RSI {p1r})  →  "
                f"Peak 2 ({p2d}): ₹{p2p} (RSI {p2r})"
            )

    return "\n".join(lines)


def format_results(results):
    lines = [
        "=" * 60,
        "📊 NSE F&O MARKET SCANNER REPORT",
        now_ist().strftime("📅 %d %b %Y | ⏰ %I:%M %p  IST"),
        "=" * 60,
    ]

    # if results.get("corp_actions_flagged"):
    #     lines.append("\n🚩 CORPORATE ACTION FLAGS (review recommended)")
    #     lines.append("-" * 60)
    #     for f in results["corp_actions_flagged"]:
    #         lines.append(f"  {f['symbol']:<15} | {f['action_type']:<10} | ex-date: {f['ex_date']:<12}")
    #         if f.get("details"):
    #             lines.append(f"      {f['details'][:80]}")
    #     lines.append("-" * 60)

    # if results.get("corp_actions_upcoming_14d"):
    #     lines.append("\n📅 UPCOMING CORP ACTIONS (next 14 days)")
    #     lines.append("-" * 60)
    #     for u in results["corp_actions_upcoming_14d"]:
    #         risky = " 🚩" if u.get("is_risky") else ""
    #         lines.append(f"  {u['ex_date']:<12} | {u['symbol']:<12} | {u['action_type']:<10}{risky}")
    #         if u.get("details"):
    #             lines.append(f"      {u['details'][:80]}")
    #     lines.append("-" * 60)

    if results.get("quarantined"):
        lines.append(f"\n⏸️  QUARANTINED: {', '.join(results['quarantined'])}")

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
            # NEW: ND callout for the index
            nd = data.get("nd_status", "none")
            if nd != "none":
                nd_emoji = "🔴" if nd == "active" else "🟡"
                nd_label = "ND ACTIVE" if nd == "active" else "ND observed"
                lines.append(f"     {nd_emoji} {nd_label} (daily price/RSI divergence)")
        else:
            lines.append(f"  {idx}: {data}")

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

            # NEW: ND callout
            nd = data.get("nd_status", "none")
            if nd != "none":
                nd_emoji = "🔴" if nd == "active" else "🟡"
                nd_label = "ND ACTIVE" if nd == "active" else "ND observed"
                lines.append(f"      {nd_emoji} {nd_label} (weekly)")

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

    lines.append("\n" + "=" * 60)
    lines.append("⚠️  Stocks marked 🚩 have recent corp actions; data may be unreliable")
    lines.append("⚠️  For informational purposes only. Trade responsibly.")
    lines.append("=" * 60)
    return "\n".join(lines)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def format_corp_action_report(results):
    """Standalone corp action report for Monday emissions."""
    lines = [
        "=" * 60,
        "📅 CORPORATE ACTIONS — NEXT 14 DAYS",
        now_ist().strftime("Generated %d %b %Y | %I:%M %p IST"),
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
    daily_name = f"scan_result_{now_ist().strftime('%d_%b_%Y')}.txt"
    with open(daily_name, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n✅ Daily report saved to {daily_name}")

    # Monday-only corp actions output
    today = now_ist()
    if today.weekday() == 0:   # Monday
        corp_name = f"corp_actions_{today.strftime('%d_%b_%Y')}.txt"
        with open(corp_name, "w", encoding="utf-8") as f:
            f.write(format_corp_action_report(results))
        print(f"📅 Monday corp actions report saved to {corp_name}")
    else:
        days_until_monday = (7 - today.weekday()) % 7 or 7
        print(f"ℹ️  Corp action report runs on Mondays (next in {days_until_monday} day(s))")
