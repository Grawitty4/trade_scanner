-- ═══════════════════════════════════════════════════════════
--  Telegram Subscribers
--  Schema-ready for tier-based distribution (free / paid / admin)
-- ═══════════════════════════════════════════════════════════

SET search_path TO trade_scanner;

CREATE TABLE IF NOT EXISTS trade_scanner.telegram_subscribers (
    chat_id           BIGINT       PRIMARY KEY,        -- Telegram user/chat ID
    username          VARCHAR(50),                     -- e.g. "@johndoe" (optional)
    full_name         VARCHAR(100),                    -- display name
    tier              VARCHAR(20)  NOT NULL DEFAULT 'paid',  -- 'free' | 'paid' | 'admin'
    opted_in          BOOLEAN      NOT NULL DEFAULT TRUE,    -- active flag (vs paused)
    is_admin          BOOLEAN      NOT NULL DEFAULT FALSE,   -- receives error alerts
    subscribed_at     TIMESTAMP    DEFAULT NOW(),
    last_message_at   TIMESTAMP,
    last_status       VARCHAR(20),                     -- last send status: 'sent' | 'failed' | 'bot_blocked'
    notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_ts_opted_in ON trade_scanner.telegram_subscribers(opted_in)
    WHERE opted_in = TRUE;
CREATE INDEX IF NOT EXISTS idx_ts_tier     ON trade_scanner.telegram_subscribers(tier);
CREATE INDEX IF NOT EXISTS idx_ts_admin    ON trade_scanner.telegram_subscribers(is_admin)
    WHERE is_admin = TRUE;
