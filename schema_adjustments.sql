-- ═══════════════════════════════════════════════════════════
--  Phase B+ : Corporate Action Adjustments
-- ═══════════════════════════════════════════════════════════
-- Safe to run multiple times (IF NOT EXISTS guards).
-- Run this once before using the adjustment scripts.

SET search_path TO trade_scanner;

-- ─────────────────────────────────────────────
-- Adjustments table
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trade_scanner.corporate_action_adjustments (
    id                  SERIAL PRIMARY KEY,
    symbol              VARCHAR(30)  NOT NULL,
    action_type         VARCHAR(20)  NOT NULL,        -- DEMERGER, SPLIT, BONUS, COMBO
    effective_date      DATE         NOT NULL,        -- prices BEFORE this date get adjusted
    price_factor        NUMERIC(12,6) NOT NULL,       -- multiply old price by this
    volume_factor       NUMERIC(12,6),                -- multiply old volume by this (inverse for splits/bonuses)
    notes               TEXT,
    discovered_at       TIMESTAMP    DEFAULT NOW(),
    applied_at          TIMESTAMP,                    -- NULL until apply runs
    applied_by_job_id   INTEGER,                      -- FK to job_runs.id (for audit)
    UNIQUE (symbol, effective_date, action_type)
);

CREATE INDEX IF NOT EXISTS idx_caa_symbol         ON trade_scanner.corporate_action_adjustments(symbol);
CREATE INDEX IF NOT EXISTS idx_caa_unapplied      ON trade_scanner.corporate_action_adjustments(applied_at) WHERE applied_at IS NULL;

-- ─────────────────────────────────────────────
-- View: symbols with pending (unapplied) adjustments
-- Used by the scanner to add a ⚠️ warning
-- ─────────────────────────────────────────────
CREATE OR REPLACE VIEW trade_scanner.pending_adjustments AS
SELECT symbol, action_type, effective_date, price_factor, notes
  FROM trade_scanner.corporate_action_adjustments
 WHERE applied_at IS NULL;
