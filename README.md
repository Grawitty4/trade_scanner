# 📊 NSE F&O Market Scanner

> **Last Updated:** 16 May 2026 (v5 — Phase B)
> **Status:** Phase B complete — Postgres-backed
> **Repo:** https://github.com/Grawitty4/trade_scanner.git

---

## 🎯 Project Goal

Automated trading research engine that scans NSE F&O segment daily (7 AM IST), and sends concise, actionable trade recommendations to Telegram.

**Top-down approach:** Market → Sectors → Stocks → Entry/SL/Target

---

## 🆕 Changelog

| Version | Phase | Changes |
|---|---|---|
| v1 | Init | Basic scanner, 10 sectors, RR ≥ 2 filter |
| v2 | – | Expanded to 17 NSE sectoral + thematic indices |
| v3 | – | Hybrid Bhavcopy + yfinance, new RSI thresholds, no RR filter |
| v4 | Phase A | yfinance-only, automated corp actions discovery + flagging |
| v4.1 | Phase A+ | Upcoming actions (7 days), dated output filename |
| **v5** | **Phase B** | **Postgres-backed (Railway), full pipeline split into 4 scripts** |

### v5 Changes
1. **Postgres on Railway** (`trade_scanner` schema, 9 tables)
2. **Split into 4 scripts**: bootstrap (one-time), scanner (daily), refresh (ad-hoc), corporate_actions (module)
3. **Incremental fetch** baked into scanner — only fetches missing days
4. **Corp actions migrated to DB** (single source of truth)
5. **Scan results audited** in `scan_results` table — ready for backtesting
6. **Job runs logged** in `job_runs` table — ready for monitoring

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────┐
│  RAILWAY                                                 │
│   ┌────────────────────────────────────────┐             │
│   │  Postgres (schema: trade_scanner)      │             │
│   │   • daily_prices    • stocks           │             │
│   │   • index_prices    • stock_sectors    │             │
│   │   • corporate_actions                  │             │
│   │   • scan_results    • job_runs         │             │
│   │   • symbol_history  • data_discrepancies│            │
│   └────────────────────────────────────────┘             │
│                  ▲                                       │
│                  │                                       │
│   ┌──────────────┴──────────────────────────┐            │
│   │  Cron Job: market_scanner.py (7 AM IST) │            │
│   │   1. Update corp actions                │            │
│   │   2. Incremental yfinance fetch         │            │
│   │   3. Run scans (from DB)                │            │
│   │   4. Persist results                    │            │
│   │   5. Write dated text report            │            │
│   │   6. (Phase D) Telegram send            │            │
│   └─────────────────────────────────────────┘            │
└──────────────────────────────────────────────────────────┘
```

---

## 📦 File Structure

```
trade_scanner/
├── .env                 # gitignored (your DATABASE_URL)
├── .env.example         # template to copy
├── .gitignore           # protects .env, outputs
├── requirements.txt     # pip dependencies
├── schema.sql           # DDL for 9 tables
├── db.py                # Connection + query helpers
├── corporate_actions.py # NSE corp action discovery (DB-backed)
├── bootstrap.py         # ONE-TIME: load max history
├── refresh_stock.py     # AD-HOC: refresh after corp actions
├── market_scanner.py    # DAILY: incremental + scan
├── README.md            # This file
└── scan_result_DD_MMM_YYYY.txt   # Dated outputs
```

---

## 🚀 Setup & First Run

### 1. Clone and set up

```bash
git clone https://github.com/Grawitty4/trade_scanner.git
cd trade_scanner

# Conda env
conda create -n trade-scanner python=3.11 -y
conda activate trade-scanner

# Install deps
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and put your real DATABASE_URL (Railway → Postgres → Public URL)
```

### 3. Initialize schema

You've already run `schema.sql` manually. The `bootstrap.py` script will also call `init_schema()` (idempotent — safe to re-run).

### 4. Bootstrap historical data (one-time)

```bash
python bootstrap.py
```

Expected: 30–60 minutes for ~200 stocks + 19 indices.
- Resumable: re-running skips stocks loaded in last 7 days
- Random sleep (0.8–1.6 s) between calls to be polite to yfinance

### 5. Run daily scan

```bash
python market_scanner.py
```

This is what will eventually run via Railway cron at 7 AM IST.

### 6. Verify DB

```bash
python db.py
# Prints: Connected: PostgreSQL ... | Stats: {...}
```

---

## 🔧 Daily Workflow

### Routine (automated)
- 7 AM IST: Railway cron triggers `market_scanner.py`
- Scanner does everything: corp action update → incremental fetch → scan → persist → report

### Exceptional (manual)
- See 🚩 flag in report (e.g., VEDL demerger)
- Decide what to do:
  - **Ignore**: stock continues being scanned (data may be unreliable until yfinance adjusts)
  - **Quarantine**: `python -c "from corporate_actions import mark_decision; mark_decision('VEDL', '30-Apr-2026', 'QUARANTINE', quarantine=True)"`
  - **Refresh**: `python refresh_stock.py VEDL` (after yfinance has adjusted)
- Or bulk refresh all flagged: `python refresh_stock.py --all-flagged`

---

## 🗄️ Schema Reference

| Table | Purpose | Key Columns |
|---|---|---|
| `stocks` | Master list | symbol PK, isin, is_active |
| `stock_sectors` | Many-to-many | symbol, sector |
| `daily_prices` | OHLCV history | (symbol, trade_date) PK |
| `index_prices` | Index OHLCV | (index_name, trade_date) PK |
| `corporate_actions` | Corp action log | unique (symbol, ex_date, action_type) |
| `symbol_history` | Rename tracker | (old_symbol, change_date) |
| `scan_results` | Daily signals | scan_date + symbol |
| `job_runs` | Operational log | job_type, status |
| `data_discrepancies` | Verification flags | for future Bhavcopy cross-check |

All tables live in the `trade_scanner` schema.

---

## 🔬 Scan Logic (unchanged from v4)

### Breakout Score (min 4/7 to qualify)
1. Price ≥ 99% of 50-day swing high
2. Volume ≥ 1.5x of 20-day average
3. Bollinger squeeze → expansion
4. MACD bullish crossover
5. Cup & Handle pattern
6. Elliott Wave 3
7. Daily RSI > 60

### GFS Strategy
Monthly RSI > 60, Weekly RSI > 60, Daily RSI **40–45**

### AGFS Strategy
Monthly RSI > 60, Weekly RSI > 60, Daily RSI **> 60**

### Must Trade 🔴
Stock in breakout sector list AND (GFS OR AGFS)

### Entry/SL/Target
Three scenarios: Breakout / Fibonacci Retracement / Watch

---

## 🚧 What's Next

**Phase C — Bhavcopy Verification (later):**
- Weekly job to cross-check yfinance vs Bhavcopy
- Auto-populate `data_discrepancies` table

**Phase D — Telegram Bot:**
- Create bot via @BotFather, save token in `.env`
- Send daily scan report
- Separate alerts for corp action flags

**Phase E — Railway Deployment:**
- Push to GitHub (already initialized)
- Connect Railway service to repo
- Set cron schedule: `0 1 * * *` (1 AM UTC = 7 AM IST)
- Set DATABASE_URL env var to internal hostname

**Phase F — Monitoring:**
- Build a simple SQL dashboard from `scan_results` + `job_runs`
- Win-rate analysis: which signals actually played out?

---

## 🔐 Security Notes

- `.env` is gitignored — never commit it
- Don't paste DATABASE_URL into chat or issue trackers
- If credentials leak, rotate the password in Railway → Postgres → Variables
- Use Railway's **internal** URL for deployment; **public** URL only for local dev

---

## ⚠️ Disclaimer

For educational and personal research only. Not financial advice. All trades carry risk.
