"""
Corporate Actions — Historical Backfill (one-time)
────────────────────────────────────────────────────
Pulls the NSE corporate actions feed in chunked windows (default 90 days)
covering the last N years (default 5). For each entry:
  • Classifies via the same keyword rules as the daily fetcher
  • Inserts into corporate_actions table (idempotent via UNIQUE constraint)

NSE API often rejects long-range queries or rate-limits aggressive scraping,
so we walk window-by-window with delays and retries.

Usage:
    python corp_actions_backfill.py                # last 5 years, 90-day windows
    python corp_actions_backfill.py --years 3
    python corp_actions_backfill.py --window 60    # 60-day windows (smaller chunks)
    python corp_actions_backfill.py --dry-run      # show what would be inserted
"""

import sys
import time
import random
import re
import requests
from datetime import datetime, timedelta

from db import test_connection, get_cursor, start_job_run, finish_job_run

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DEFAULT_YEARS  = 5
DEFAULT_WINDOW = 90      # days per request
MAX_RETRIES    = 3
RETRY_BACKOFF  = 8       # seconds, doubled each retry

NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept":     "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":    "https://www.nseindia.com/companies-listing/corporate-filings-actions",
    "Connection": "keep-alive",
}

# Same rules as corporate_actions.py — keep aligned
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


def classify_action(text):
    if not text:
        return "UNKNOWN", False
    text_lower = text.lower()
    for pattern, action_type, is_risky in CLASSIFICATION_RULES:
        if re.search(pattern, text_lower):
            return action_type, is_risky
    return "OTHER", False


# ─────────────────────────────────────────────
# NSE SESSION
# ─────────────────────────────────────────────
def _new_session():
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    try:
        s.get("https://www.nseindia.com", timeout=10)
        s.get("https://www.nseindia.com/companies-listing/corporate-filings-actions",
              timeout=10)
    except Exception:
        pass
    return s


def fetch_window(session, from_date, to_date):
    """Fetch one window. Returns list (possibly empty) or None on failure."""
    url = (f"https://www.nseindia.com/api/corporates-corporateActions"
           f"?index=equities"
           f"&from_date={from_date.strftime('%d-%m-%Y')}"
           f"&to_date={to_date.strftime('%d-%m-%Y')}")

    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, timeout=25)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
                return []
            if resp.status_code in (429, 503):
                wait = RETRY_BACKOFF * (2 ** attempt)
                print(f"      ⚠️  HTTP {resp.status_code}; cooling down {wait}s...")
                time.sleep(wait)
                # Refresh session cookies on rate-limit
                session = _new_session()
                continue
            print(f"      ⚠️  HTTP {resp.status_code}")
            return None
        except requests.RequestException as e:
            wait = RETRY_BACKOFF * (2 ** attempt)
            print(f"      ⚠️  Error: {e}; retrying in {wait}s...")
            time.sleep(wait)
        except ValueError as e:
            # JSON parse failed
            print(f"      ⚠️  Bad JSON: {e}")
            return None
    return None


# ─────────────────────────────────────────────
# DB INSERT (idempotent)
# ─────────────────────────────────────────────
def _insert_action(cur, entry):
    symbol  = (entry.get("symbol") or "").strip()
    company = (entry.get("comp") or "").strip()
    purpose = ((entry.get("subject") or entry.get("purpose") or "")).strip()
    ex_date = (entry.get("exDate") or entry.get("ex_date") or "").strip()
    record  = (entry.get("recDate") or entry.get("record_date") or "").strip()

    if not symbol or not purpose:
        return False, None

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
    return inserted is not None, action_type


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args

    years   = DEFAULT_YEARS
    window  = DEFAULT_WINDOW
    if "--years" in args:
        i = args.index("--years")
        if i + 1 < len(args):
            try: years = int(args[i + 1])
            except: pass
    if "--window" in args:
        i = args.index("--window")
        if i + 1 < len(args):
            try: window = int(args[i + 1])
            except: pass

    today = datetime.now().date()
    start = today - timedelta(days=365 * years)

    print("=" * 65)
    print("  CORPORATE ACTIONS — HISTORICAL BACKFILL")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print(f"  Range: {start} → {today}  ({years} years)")
    print(f"  Window: {window} days per request")
    print(f"  Mode: {'DRY-RUN' if dry_run else 'COMMIT'}")
    print("=" * 65)

    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return

    session = _new_session()
    job_id = None
    if not dry_run:
        job_id = start_job_run(
            "BACKFILL_CORP_ACTIONS",
            notes=f"years={years} window={window}",
        )

    total_entries  = 0
    total_inserted = 0
    type_counts = {}
    window_count = 0
    failures = []

    cur_from = start
    while cur_from <= today:
        cur_to = min(cur_from + timedelta(days=window - 1), today)
        window_count += 1
        print(f"\n[{window_count}] {cur_from} → {cur_to}")

        entries = fetch_window(session, cur_from, cur_to)
        if entries is None:
            print(f"   ❌ Window failed after retries.")
            failures.append((cur_from, cur_to))
            cur_from = cur_to + timedelta(days=1)
            time.sleep(random.uniform(2.0, 4.0))
            continue

        print(f"   📥 Received {len(entries)} entries")
        total_entries += len(entries)

        inserted_here = 0
        if not dry_run and entries:
            with get_cursor() as (_, cur):
                for entry in entries:
                    try:
                        was_new, action_type = _insert_action(cur, entry)
                        if was_new:
                            inserted_here += 1
                            type_counts[action_type] = type_counts.get(action_type, 0) + 1
                    except Exception as e:
                        # Don't let one bad entry abort the window
                        pass
        elif dry_run and entries:
            # Just count what types we'd insert
            for entry in entries:
                purpose = entry.get("subject") or entry.get("purpose") or ""
                action_type, _ = classify_action(purpose)
                type_counts[action_type] = type_counts.get(action_type, 0) + 1

        total_inserted += inserted_here
        if not dry_run:
            print(f"   ✅ Inserted {inserted_here} new (skipped {len(entries) - inserted_here} duplicates)")

        # polite sleep between windows
        time.sleep(random.uniform(2.5, 4.5))
        cur_from = cur_to + timedelta(days=1)

    # Summary
    print("\n" + "=" * 65)
    print("  SUMMARY")
    print("=" * 65)
    print(f"  Windows queried        : {window_count}")
    print(f"  Total entries received : {total_entries}")
    print(f"  {'Would insert' if dry_run else 'Inserted (new)'}: {total_inserted}")
    if type_counts:
        print(f"  By action type:")
        for action_type, cnt in sorted(type_counts.items(), key=lambda x: -x[1]):
            print(f"     {action_type:<12} {cnt}")
    if failures:
        print(f"\n  ⚠️  {len(failures)} window(s) failed:")
        for f, t in failures:
            print(f"     {f} → {t}")
        print(f"  Re-run later to retry — successful inserts won't be duplicated.")

    if job_id:
        finish_job_run(
            job_id,
            "SUCCESS" if not failures else "PARTIAL",
            stocks_processed=total_inserted,
            error_message=f"failed_windows={len(failures)}" if failures else None,
        )

    if not dry_run:
        print(f"\n   Next: python auto_compute_factors.py --dry-run --years {years}")


if __name__ == "__main__":
    main()
