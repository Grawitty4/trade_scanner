"""
Corporate Actions Module — DB-backed (Phase B)
───────────────────────────────────────────────
Same public API as v4 (Phase A), but persists to Postgres instead of JSON.
"""

import re
import json
import requests
from datetime import datetime, timedelta
from pathlib import Path

from db import get_cursor

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
QUARANTINE_DAYS = 60
LOOKBACK_DAYS   = 30
LOOKAHEAD_DAYS  = 30

NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept":     "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":    "https://www.nseindia.com/companies-listing/corporate-filings-actions",
    "Connection": "keep-alive",
}

CLASSIFICATION_RULES = [
    (r"\bdemerg(er|ed|ing)\b",                              "DEMERGER", True),
    (r"\bscheme of arrangement\b",                          "DEMERGER", True),
    (r"\b(merger|amalgamation|acquisition)\b",              "MERGER",   True),
    (r"\b(name change|change of name|renamed|new name)\b",  "RENAME",   True),
    (r"\b(spin[- ]?off|hive[- ]?off)\b",                    "DEMERGER", True),
    (r"\bstock split\b",                                    "SPLIT",    False),
    (r"\bsub[- ]?division\b",                               "SPLIT",    False),
    (r"face value.*(split|sub-?divid|from.*to)",            "SPLIT",    False),
    (r"\bbonus\b",                                          "BONUS",    False),
    (r"\bbuy[- ]?back\b",                                   "BUYBACK",  False),
    (r"\brights\b",                                         "RIGHTS",   False),
    (r"\b(interim|final|special)?\s*dividend\b",            "DIVIDEND", False),
]


# ─────────────────────────────────────────────
# CLASSIFICATION
# ─────────────────────────────────────────────
def classify_action(text):
    if not text:
        return "UNKNOWN", False
    text_lower = text.lower()
    for pattern, action_type, is_risky in CLASSIFICATION_RULES:
        if re.search(pattern, text_lower):
            return action_type, is_risky
    return "OTHER", False


# ─────────────────────────────────────────────
# NSE FETCH
# ─────────────────────────────────────────────
def fetch_nse_corporate_actions():
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=10)
        session.get("https://www.nseindia.com/companies-listing/corporate-filings-actions",
                    timeout=10)
    except Exception:
        pass

    today = datetime.now()
    from_date = (today - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%m-%Y")
    to_date   = (today + timedelta(days=LOOKAHEAD_DAYS)).strftime("%d-%m-%Y")
    url = (f"https://www.nseindia.com/api/corporates-corporateActions"
           f"?index=equities&from_date={from_date}&to_date={to_date}")

    try:
        resp = session.get(url, timeout=20)
        if resp.status_code != 200:
            print(f"   ⚠️  NSE API status {resp.status_code}")
            return []
        data = resp.json()
        if not isinstance(data, list):
            print(f"   ⚠️  Unexpected NSE API response format")
            return []
        print(f"   ✅ NSE corp actions fetched: {len(data)} entries")
        return data
    except Exception as e:
        print(f"   ⚠️  NSE corp actions fetch failed: {e}")
        return []


# ─────────────────────────────────────────────
# DATE PARSING
# ─────────────────────────────────────────────
def _parse_ex_date(date_str):
    if not date_str:
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except Exception:
            continue
    return None


# ─────────────────────────────────────────────
# UPDATE WORKFLOW
# ─────────────────────────────────────────────
def update_corporate_actions():
    print("\n📋 Updating Corporate Actions...")
    raw_actions = fetch_nse_corporate_actions()
    new_risky_flags = []
    added_count = 0

    with get_cursor() as (_, cur):
        for entry in raw_actions:
            symbol  = (entry.get("symbol") or "").strip()
            company = (entry.get("comp") or "").strip()
            purpose = ((entry.get("subject") or entry.get("purpose") or "")).strip()
            ex_date = (entry.get("exDate") or entry.get("ex_date") or "").strip()
            record  = (entry.get("recDate") or entry.get("record_date") or "").strip()

            if not symbol or not purpose:
                continue

            action_type, is_risky = classify_action(purpose)

            cur.execute("""
                INSERT INTO corporate_actions
                    (symbol, company_name, action_type, raw_text,
                     ex_date, record_date, is_risky, user_decision)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, ex_date, action_type) DO NOTHING
                RETURNING id
            """, (symbol, company, action_type, purpose, ex_date, record,
                  is_risky, "PENDING" if is_risky else "AUTO_OK"))
            inserted = cur.fetchone()
            if inserted:
                added_count += 1
                if is_risky:
                    new_risky_flags.append({
                        "symbol": symbol, "action_type": action_type,
                        "ex_date": ex_date, "details": purpose,
                    })

        cur.execute("""
            SELECT symbol, action_type, ex_date, raw_text
            FROM corporate_actions
            WHERE is_risky = TRUE AND user_decision = 'PENDING'
            ORDER BY discovered_at DESC
        """)
        risky_pending = [
            {"symbol": r[0], "action_type": r[1], "ex_date": r[2], "details": r[3]}
            for r in cur.fetchall()
        ]

    print(f"   ✅ {added_count} new corp actions added")
    print(f"   ⚠️  {len(new_risky_flags)} new RISKY events (need review)")
    print(f"   📌 {len(risky_pending)} total RISKY events pending decision")

    return {
        "new_risky":     new_risky_flags,
        "total_actions": added_count,
        "risky_pending": risky_pending,
    }


# ─────────────────────────────────────────────
# QUERY HELPERS (used by scanner)
# ─────────────────────────────────────────────
def get_quarantined_symbols():
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT DISTINCT symbol FROM corporate_actions
            WHERE quarantine_until IS NOT NULL
              AND quarantine_until >= CURRENT_DATE
        """)
        return {r[0] for r in cur.fetchall()}


def get_flagged_symbols():
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT symbol, action_type, ex_date, raw_text
            FROM corporate_actions
            WHERE is_risky = TRUE AND user_decision = 'PENDING'
            ORDER BY discovered_at DESC
        """)
        return [
            {"symbol": r[0], "action_type": r[1], "ex_date": r[2], "details": r[3]}
            for r in cur.fetchall()
        ]


def get_upcoming_actions(days_ahead=7):
    """
    All actions with ex_date within next `days_ahead` days, sorted asc.
    """
    today  = datetime.now().date()
    cutoff = today + timedelta(days=days_ahead)
    upcoming = []

    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT symbol, company_name, action_type, ex_date, is_risky, raw_text
            FROM corporate_actions
        """)
        for row in cur.fetchall():
            ex = _parse_ex_date(row[3])
            if ex and today <= ex <= cutoff:
                upcoming.append({
                    "symbol":      row[0],
                    "company":     row[1] or "",
                    "action_type": row[2],
                    "ex_date":     row[3],
                    "ex_date_obj": ex,
                    "is_risky":    row[4],
                    "details":     row[5] or "",
                })

    upcoming.sort(key=lambda x: x["ex_date_obj"])
    return upcoming


def mark_decision(symbol, ex_date, decision, quarantine=False):
    q_until = None
    if quarantine:
        q_until = (datetime.now() + timedelta(days=QUARANTINE_DAYS)).date()
    with get_cursor() as (_, cur):
        cur.execute("""
            UPDATE corporate_actions
            SET user_decision = %s, quarantine_until = %s
            WHERE symbol = %s AND ex_date = %s
        """, (decision, q_until, symbol, ex_date))


# ─────────────────────────────────────────────
# JSON MIGRATION (one-off helper)
# ─────────────────────────────────────────────
def migrate_from_json(json_path="corporate_actions.json"):
    """Import existing JSON state into DB. Idempotent — re-running is safe."""
    if not Path(json_path).exists():
        print(f"   ℹ️  No {json_path} to migrate")
        return 0
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    actions = data.get("actions", [])
    if not actions:
        return 0

    inserted = 0
    with get_cursor() as (_, cur):
        for a in actions:
            q_until = a.get("quarantine_until")
            q_date = None
            if q_until:
                try:
                    q_date = datetime.fromisoformat(q_until).date()
                except Exception:
                    q_date = None
            cur.execute("""
                INSERT INTO corporate_actions
                    (symbol, company_name, action_type, raw_text,
                     ex_date, record_date, is_risky, user_decision,
                     quarantine_until)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, ex_date, action_type) DO NOTHING
                RETURNING id
            """, (
                a["symbol"], a.get("company_name", ""),
                a.get("action_type", "UNKNOWN"), a.get("raw_text", ""),
                a.get("ex_date", ""), a.get("record_date", ""),
                a.get("is_risky", False),
                a.get("user_decision", "PENDING"),
                q_date,
            ))
            if cur.fetchone():
                inserted += 1
    print(f"   ✅ Migrated {inserted} corp actions from JSON")
    return inserted


# ─────────────────────────────────────────────
# REPORTING
# ─────────────────────────────────────────────
def format_flag_report(flagged):
    if not flagged:
        return "\n🟢 No pending corp action flags."
    lines = [f"\n🚩 PENDING CORP ACTION REVIEW ({len(flagged)} items)"]
    lines.append("─" * 60)
    for f in flagged:
        lines.append(
            f"  • {f['symbol']:<15} | {f['action_type']:<10} | "
            f"ex-date: {f['ex_date']:<12} | {f['details'][:60]}"
        )
    lines.append("─" * 60)
    return "\n".join(lines)


if __name__ == "__main__":
    result = update_corporate_actions()
    print(format_flag_report(result["risky_pending"]))
