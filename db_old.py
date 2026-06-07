"""
Database connection & query helpers
────────────────────────────────────
- Reads DATABASE_URL from .env
- Provides connection management via context manager
- Common query helpers used across scanner / bootstrap / refresh scripts
"""

import os
import pandas as pd
from datetime import datetime, date
from contextlib import contextmanager
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Create a .env file with: "
        "DATABASE_URL=postgresql://user:pass@host:port/dbname"
    )

SCHEMA = "trade_scanner"


# ─────────────────────────────────────────────
# CONNECTION MANAGEMENT
# ─────────────────────────────────────────────
@contextmanager
def get_conn():
    """Yields a Postgres connection, commits on success, rolls back on error."""
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_cursor(dict_cursor=False):
    """Convenience context: yields (conn, cursor)."""
    with get_conn() as conn:
        cursor_factory = RealDictCursor if dict_cursor else None
        cur = conn.cursor(cursor_factory=cursor_factory)
        try:
            cur.execute(f"SET search_path TO {SCHEMA};")
            yield conn, cur
        finally:
            cur.close()


def test_connection():
    """Smoke test — returns server version on success, raises on failure."""
    with get_cursor() as (_, cur):
        cur.execute("SELECT version();")
        return cur.fetchone()[0]


# ─────────────────────────────────────────────
# SCHEMA MANAGEMENT
# ─────────────────────────────────────────────
def init_schema(schema_sql_path="schema.sql"):
    """Apply schema.sql. Safe to run multiple times (CREATE IF NOT EXISTS)."""
    with open(schema_sql_path, "r", encoding="utf-8") as f:
        sql = f.read()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        cur.close()
    print(f"   ✅ Schema '{SCHEMA}' initialized / verified")


# ─────────────────────────────────────────────
# STOCKS / SECTORS
# ─────────────────────────────────────────────
def upsert_stock(symbol, yfinance_ticker=None, company_name=None,
                 isin=None, is_fno=True, is_active=True):
    """Insert or update stock master record."""
    with get_cursor() as (_, cur):
        cur.execute("""
            INSERT INTO stocks (symbol, yfinance_ticker, company_name, isin,
                                is_fno, is_active, last_data_refresh)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (symbol) DO UPDATE SET
                yfinance_ticker = COALESCE(EXCLUDED.yfinance_ticker, stocks.yfinance_ticker),
                company_name    = COALESCE(EXCLUDED.company_name,    stocks.company_name),
                isin            = COALESCE(EXCLUDED.isin,            stocks.isin),
                is_fno          = EXCLUDED.is_fno,
                is_active       = EXCLUDED.is_active,
                updated_at      = NOW()
        """, (symbol, yfinance_ticker, company_name, isin, is_fno, is_active))


def upsert_sector_mapping(symbol, sectors):
    """Replace sector mapping for a symbol."""
    with get_cursor() as (_, cur):
        cur.execute("DELETE FROM stock_sectors WHERE symbol = %s;", (symbol,))
        if sectors:
            execute_values(
                cur,
                "INSERT INTO stock_sectors (symbol, sector) VALUES %s",
                [(symbol, s) for s in sectors],
            )


def get_all_stocks(active_only=True):
    """Returns list of stock symbols."""
    sql = "SELECT symbol FROM stocks"
    if active_only:
        sql += " WHERE is_active = TRUE"
    sql += " ORDER BY symbol;"
    with get_cursor() as (_, cur):
        cur.execute(sql)
        return [r[0] for r in cur.fetchall()]


def get_stocks_by_sector(sector):
    """Returns symbols for a sector."""
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT s.symbol FROM stocks s
            JOIN stock_sectors ss ON ss.symbol = s.symbol
            WHERE ss.sector = %s AND s.is_active = TRUE
            ORDER BY s.symbol
        """, (sector,))
        return [r[0] for r in cur.fetchall()]


def mark_data_refreshed(symbol):
    with get_cursor() as (_, cur):
        cur.execute("UPDATE stocks SET last_data_refresh = NOW() WHERE symbol = %s",
                    (symbol,))


# ─────────────────────────────────────────────
# DAILY PRICES
# ─────────────────────────────────────────────
def get_latest_trade_date(symbol):
    """Returns the latest trade_date for a symbol, or None."""
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT MAX(trade_date) FROM daily_prices WHERE symbol = %s
        """, (symbol,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None


def insert_daily_prices(symbol, df):
    """
    Bulk insert/upsert OHLCV data.
    df must have columns: Open, High, Low, Close, Volume and a DatetimeIndex.
    """
    if df is None or df.empty:
        return 0

    rows = []
    for idx, row in df.iterrows():
        trade_dt = idx.date() if hasattr(idx, "date") else idx
        try:
            rows.append((
                symbol,
                trade_dt,
                float(row['Open']),
                float(row['High']),
                float(row['Low']),
                float(row['Close']),
                int(row['Volume']) if pd.notna(row['Volume']) else 0,
            ))
        except (TypeError, ValueError):
            continue  # skip malformed rows silently

    if not rows:
        return 0

    with get_cursor() as (_, cur):
        execute_values(
            cur,
            """
            INSERT INTO daily_prices (symbol, trade_date, open, high, low, close, volume)
            VALUES %s
            ON CONFLICT (symbol, trade_date) DO UPDATE SET
                open   = EXCLUDED.open,
                high   = EXCLUDED.high,
                low    = EXCLUDED.low,
                close  = EXCLUDED.close,
                volume = EXCLUDED.volume,
                fetched_at = NOW()
            """,
            rows,
        )
    return len(rows)


def fetch_prices_df(symbol, start_date=None, limit=None):
    """
    Fetch OHLCV history for a symbol as a DataFrame (DatetimeIndex).
    Used by the scanner so it doesn't hit yfinance for analysis.
    """
    sql = """
        SELECT trade_date, open, high, low, close, volume
        FROM daily_prices WHERE symbol = %s
    """
    params = [symbol]
    if start_date:
        sql += " AND trade_date >= %s"
        params.append(start_date)
    sql += " ORDER BY trade_date ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    with get_cursor() as (_, cur):
        cur.execute(sql, params)
        rows = cur.fetchall()

    if not rows:
        return None

    df = pd.DataFrame(rows, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
    df['Date']  = pd.to_datetime(df['Date'])
    df          = df.set_index('Date')
    df          = df.astype({'Open': float, 'High': float, 'Low': float,
                             'Close': float, 'Volume': float})
    return df


def delete_prices(symbol):
    """Delete all daily_prices for a symbol. Used by refresh_stock.py."""
    with get_cursor() as (_, cur):
        cur.execute("DELETE FROM daily_prices WHERE symbol = %s", (symbol,))
        return cur.rowcount


# ─────────────────────────────────────────────
# INDEX PRICES
# ─────────────────────────────────────────────
def get_latest_index_date(index_name):
    with get_cursor() as (_, cur):
        cur.execute("SELECT MAX(trade_date) FROM index_prices WHERE index_name = %s",
                    (index_name,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None


def insert_index_prices(index_name, df):
    if df is None or df.empty:
        return 0
    rows = []
    for idx, row in df.iterrows():
        trade_dt = idx.date() if hasattr(idx, "date") else idx
        try:
            rows.append((
                index_name, trade_dt,
                float(row['Open']), float(row['High']),
                float(row['Low']),  float(row['Close']),
            ))
        except (TypeError, ValueError):
            continue
    if not rows:
        return 0
    with get_cursor() as (_, cur):
        execute_values(
            cur,
            """
            INSERT INTO index_prices (index_name, trade_date, open, high, low, close)
            VALUES %s
            ON CONFLICT (index_name, trade_date) DO UPDATE SET
                open  = EXCLUDED.open,
                high  = EXCLUDED.high,
                low   = EXCLUDED.low,
                close = EXCLUDED.close,
                fetched_at = NOW()
            """,
            rows,
        )
    return len(rows)


def fetch_index_df(index_name, start_date=None):
    sql = "SELECT trade_date, open, high, low, close FROM index_prices WHERE index_name = %s"
    params = [index_name]
    if start_date:
        sql += " AND trade_date >= %s"
        params.append(start_date)
    sql += " ORDER BY trade_date ASC"
    with get_cursor() as (_, cur):
        cur.execute(sql, params)
        rows = cur.fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=['Date', 'Open', 'High', 'Low', 'Close'])
    df['Date'] = pd.to_datetime(df['Date'])
    df = df.set_index('Date').astype(float)
    return df


# ─────────────────────────────────────────────
# JOB RUNS
# ─────────────────────────────────────────────
def start_job_run(job_type, notes=None):
    with get_cursor() as (_, cur):
        cur.execute("""
            INSERT INTO job_runs (run_date, job_type, started_at, status, notes)
            VALUES (CURRENT_DATE, %s, NOW(), 'RUNNING', %s)
            RETURNING id
        """, (job_type, notes))
        return cur.fetchone()[0]


def finish_job_run(job_id, status, stocks_processed=0, signals_found=0,
                   error_message=None):
    with get_cursor() as (_, cur):
        cur.execute("""
            UPDATE job_runs SET
                completed_at = NOW(),
                status = %s,
                stocks_processed = %s,
                signals_found = %s,
                error_message = %s
            WHERE id = %s
        """, (status, stocks_processed, signals_found, error_message, job_id))


# ─────────────────────────────────────────────
# SCAN RESULTS
# ─────────────────────────────────────────────
def save_scan_result(scan_date, stock_data):
    """
    Persist a single scan result.
    stock_data should match the dict produced by scan_stock().
    """
    def _parse_money(s):
        if isinstance(s, (int, float)):
            return float(s)
        if not isinstance(s, str):
            return None
        return float(s.replace("₹", "").replace(",", "").strip())

    # Entry zone "₹148 – ₹149"
    entry_low, entry_high = None, None
    ez = stock_data.get("entry_zone", "")
    if "–" in ez:
        parts = [p.strip() for p in ez.split("–")]
        try:
            entry_low  = _parse_money(parts[0])
            entry_high = _parse_money(parts[1])
        except Exception:
            pass

    score = stock_data.get("breakout_score", "0/7")
    try:
        score_int = int(score.split("/")[0])
    except Exception:
        score_int = None

    with get_cursor() as (_, cur):
        cur.execute("""
            INSERT INTO scan_results (
                scan_date, symbol, sector, breakout_score,
                rsi_daily, rsi_weekly, rsi_monthly,
                is_gfs, is_agfs, is_must_trade,
                entry_type, entry_low, entry_high,
                stop_loss, target1, target2, rr_ratio
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            scan_date,
            stock_data.get("ticker"),
            stock_data.get("sector"),
            score_int,
            stock_data.get("rsi_daily"),
            stock_data.get("rsi_weekly"),
            stock_data.get("rsi_monthly"),
            stock_data.get("gfs", False),
            stock_data.get("agfs", False),
            stock_data.get("must_trade", False),
            stock_data.get("entry_type"),
            entry_low,
            entry_high,
            _parse_money(stock_data.get("stop_loss")),
            _parse_money(stock_data.get("target1")),
            _parse_money(stock_data.get("target2")),
            stock_data.get("rr_ratio"),
        ))


# ─────────────────────────────────────────────
# QUICK STATS (handy for monitoring)
# ─────────────────────────────────────────────
def quick_stats():
    with get_cursor(dict_cursor=True) as (_, cur):
        cur.execute("""
            SELECT
              (SELECT COUNT(*) FROM stocks)             AS stocks,
              (SELECT COUNT(*) FROM stocks WHERE is_active) AS active_stocks,
              (SELECT COUNT(*) FROM daily_prices)       AS price_rows,
              (SELECT COUNT(*) FROM index_prices)       AS index_rows,
              (SELECT COUNT(*) FROM corporate_actions)  AS corp_actions,
              (SELECT COUNT(*) FROM scan_results)       AS scan_results,
              (SELECT MAX(trade_date) FROM daily_prices) AS latest_data
        """)
        return dict(cur.fetchone())


if __name__ == "__main__":
    print("Testing DB connection...")
    try:
        version = test_connection()
        print(f"✅ Connected: {version[:60]}...")
        print("\nStats:", quick_stats())
    except Exception as e:
        print(f"❌ Connection failed: {e}")
