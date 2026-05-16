-- ═══════════════════════════════════════════════════════════
--  NSE F&O Scanner — Database Schema
--  Schema: trade_scanner
--  Created in Phase B
-- ═══════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS trade_scanner;
SET search_path TO trade_scanner;

-- ─────────────────────────────────────────────
-- 1. STOCKS (master list)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trade_scanner.stocks (
    symbol              VARCHAR(30) PRIMARY KEY,
    yfinance_ticker     VARCHAR(40),
    company_name        VARCHAR(200),
    isin                VARCHAR(20),
    listing_date        DATE,
    is_fno              BOOLEAN DEFAULT TRUE,
    is_active           BOOLEAN DEFAULT TRUE,
    last_data_refresh   TIMESTAMP,
    data_quality_flag   VARCHAR(20) DEFAULT 'OK',
    notes               TEXT,
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- 2. STOCK-SECTOR MAPPING (many-to-many)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trade_scanner.stock_sectors (
    symbol  VARCHAR(30) NOT NULL REFERENCES trade_scanner.stocks(symbol) ON DELETE CASCADE,
    sector  VARCHAR(50) NOT NULL,
    PRIMARY KEY (symbol, sector)
);
CREATE INDEX IF NOT EXISTS idx_ss_sector ON trade_scanner.stock_sectors(sector);

-- ─────────────────────────────────────────────
-- 3. DAILY PRICES (main OHLCV table)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trade_scanner.daily_prices (
    symbol      VARCHAR(30) NOT NULL REFERENCES trade_scanner.stocks(symbol) ON DELETE CASCADE,
    trade_date  DATE NOT NULL,
    open        NUMERIC(12,2),
    high        NUMERIC(12,2),
    low         NUMERIC(12,2),
    close       NUMERIC(12,2),
    volume      BIGINT,
    fetched_at  TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_dp_date           ON trade_scanner.daily_prices(trade_date);
CREATE INDEX IF NOT EXISTS idx_dp_symbol_date    ON trade_scanner.daily_prices(symbol, trade_date DESC);

-- ─────────────────────────────────────────────
-- 4. INDEX PRICES (NIFTY, SENSEX, sector indices)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trade_scanner.index_prices (
    index_name  VARCHAR(50) NOT NULL,
    trade_date  DATE NOT NULL,
    open        NUMERIC(12,2),
    high        NUMERIC(12,2),
    low         NUMERIC(12,2),
    close       NUMERIC(12,2),
    fetched_at  TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (index_name, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_ip_date ON trade_scanner.index_prices(trade_date);

-- ─────────────────────────────────────────────
-- 5. CORPORATE ACTIONS
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trade_scanner.corporate_actions (
    id                  SERIAL PRIMARY KEY,
    symbol              VARCHAR(30) NOT NULL,
    company_name        VARCHAR(200),
    action_type         VARCHAR(20) NOT NULL,
    raw_text            TEXT,
    ex_date             VARCHAR(20),
    record_date         VARCHAR(20),
    is_risky            BOOLEAN DEFAULT FALSE,
    user_decision       VARCHAR(20) DEFAULT 'PENDING',
    quarantine_until    DATE,
    discovered_at       TIMESTAMP DEFAULT NOW(),
    UNIQUE (symbol, ex_date, action_type)
);
CREATE INDEX IF NOT EXISTS idx_ca_symbol         ON trade_scanner.corporate_actions(symbol);
CREATE INDEX IF NOT EXISTS idx_ca_decision       ON trade_scanner.corporate_actions(user_decision);
CREATE INDEX IF NOT EXISTS idx_ca_risky          ON trade_scanner.corporate_actions(is_risky)
    WHERE is_risky = TRUE;

-- ─────────────────────────────────────────────
-- 6. SYMBOL HISTORY (rename tracking)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trade_scanner.symbol_history (
    id              SERIAL PRIMARY KEY,
    old_symbol      VARCHAR(30) NOT NULL,
    new_symbol      VARCHAR(30) NOT NULL,
    change_date     DATE NOT NULL,
    reason          VARCHAR(100),
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (old_symbol, change_date)
);
CREATE INDEX IF NOT EXISTS idx_sh_new ON trade_scanner.symbol_history(new_symbol);

-- ─────────────────────────────────────────────
-- 7. SCAN RESULTS (signal audit & backtesting)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trade_scanner.scan_results (
    id              SERIAL PRIMARY KEY,
    scan_date       DATE NOT NULL,
    symbol          VARCHAR(30) NOT NULL,
    sector          VARCHAR(50),
    breakout_score  INTEGER,
    rsi_daily       NUMERIC(5,2),
    rsi_weekly      NUMERIC(5,2),
    rsi_monthly     NUMERIC(5,2),
    is_gfs          BOOLEAN DEFAULT FALSE,
    is_agfs         BOOLEAN DEFAULT FALSE,
    is_must_trade   BOOLEAN DEFAULT FALSE,
    entry_type      VARCHAR(30),
    entry_low       NUMERIC(12,2),
    entry_high      NUMERIC(12,2),
    stop_loss       NUMERIC(12,2),
    target1         NUMERIC(12,2),
    target2         NUMERIC(12,2),
    rr_ratio        NUMERIC(5,2),
    created_at      TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sr_date    ON trade_scanner.scan_results(scan_date);
CREATE INDEX IF NOT EXISTS idx_sr_symbol  ON trade_scanner.scan_results(symbol);

-- ─────────────────────────────────────────────
-- 8. JOB RUNS (operational audit)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trade_scanner.job_runs (
    id              SERIAL PRIMARY KEY,
    run_date        DATE NOT NULL,
    job_type        VARCHAR(30) NOT NULL,
    started_at      TIMESTAMP DEFAULT NOW(),
    completed_at    TIMESTAMP,
    status          VARCHAR(20),
    stocks_processed INTEGER DEFAULT 0,
    signals_found   INTEGER DEFAULT 0,
    error_message   TEXT,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_jr_date ON trade_scanner.job_runs(run_date DESC);
CREATE INDEX IF NOT EXISTS idx_jr_type ON trade_scanner.job_runs(job_type);

-- ─────────────────────────────────────────────
-- 9. DATA DISCREPANCIES (Bhavcopy verification, future use)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trade_scanner.data_discrepancies (
    id                  SERIAL PRIMARY KEY,
    symbol              VARCHAR(30),
    trade_date          DATE,
    yfinance_close      NUMERIC(12,2),
    bhavcopy_close      NUMERIC(12,2),
    diff_percent        NUMERIC(5,2),
    flagged_at          TIMESTAMP DEFAULT NOW(),
    resolved            BOOLEAN DEFAULT FALSE,
    resolution_notes    TEXT
);
CREATE INDEX IF NOT EXISTS idx_dd_unresolved ON trade_scanner.data_discrepancies(resolved)
    WHERE resolved = FALSE;