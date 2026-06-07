"""
Expand Stock Universe to NIFTY 500
────────────────────────────────────
Downloads NSE's authoritative ind_nifty500list.csv, parses it, and:
  • Inserts new stocks into `stocks` table (with is_fno=False since they're
    being added as investment universe, not trading)
  • Auto-creates sector mappings from the CSV's "Industry" column
  • Marks existing F&O stocks with is_fno=True (preserved)
  • Existing rows are NOT modified beyond sector mapping enrichment

Idempotent: re-running is safe; only new stocks are added.

Usage:
    python expand_to_nifty500.py            # dry-run preview
    python expand_to_nifty500.py --commit   # actually insert
"""

import sys
import io
import requests
import csv
from datetime import datetime
from db import (
    test_connection,
    get_cursor,
    upsert_stock,
    upsert_sector_mapping,
    start_job_run,
    finish_job_run,
)

NSE_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/csv,application/csv,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}


def fetch_nifty500_csv():
    """Download the official NIFTY 500 constituents CSV from NSE."""
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    # Prime cookies on NSE main site (their CDN sometimes requires it)
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception:
        pass

    try:
        resp = session.get(NSE_URL, timeout=30)
        if resp.status_code != 200:
            print(f"   ❌ HTTP {resp.status_code} from NSE")
            return None
        return resp.text
    except Exception as e:
        print(f"   ❌ Fetch failed: {e}")
        return None


def parse_csv(csv_text):
    """
    Parse NSE's NIFTY 500 CSV.
    Expected columns: Company Name, Industry, Symbol, Series, ISIN Code
    Returns list of dicts with normalized keys.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = []
    for row in reader:
        # NSE column headers have spaces and case variations — normalize
        normalized = {
            k.strip().lower().replace(" ", "_"): (v.strip() if isinstance(v, str) else v)
            for k, v in row.items()
        }
        rows.append(normalized)
    return rows


def get_existing_symbols():
    """Returns set of symbols already in our `stocks` table."""
    with get_cursor() as (_, cur):
        cur.execute("SELECT symbol FROM stocks")
        return {r[0] for r in cur.fetchall()}


def industry_to_sector(industry):
    """
    Map NSE's 'Industry' to a Nifty sector name (matching our existing
    sector indices where possible). Anything else stays as the raw industry.
    """
    if not industry:
        return "Other"

    industry_lower = industry.lower()
    # Map common ones
    mapping = {
        "financial services":     "Nifty Financial Services",
        "information technology": "Nifty IT",
        "oil gas & consumable fuels": "Nifty Oil & Gas",
        "fast moving consumer goods": "Nifty FMCG",
        "healthcare":             "Nifty Healthcare",
        "automobile and auto components": "Nifty Auto",
        "metals & mining":        "Nifty Metal",
        "power":                  "Nifty Energy",
        "consumer durables":      "Nifty Consumer Durables",
        "realty":                 "Nifty Realty",
        "chemicals":              "Nifty Chemicals",
        "media entertainment & publication": "Nifty Media",
        "construction":           "Nifty Infra",
        "construction materials": "Nifty Infra",
    }
    for key, val in mapping.items():
        if key in industry_lower:
            return val
    # Otherwise, prefix raw industry for clarity
    return f"Other - {industry}"


def main():
    args = sys.argv[1:]
    commit = "--commit" in args

    print("=" * 65)
    print("  EXPAND STOCK UNIVERSE TO NIFTY 500")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print(f"  Mode: {'COMMIT' if commit else 'DRY-RUN'}")
    print("=" * 65)

    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return

    print("\n[1/3] Downloading NIFTY 500 CSV from NSE...")
    csv_text = fetch_nifty500_csv()
    if not csv_text:
        print("❌ Failed to download. Try again later or check NSE accessibility.")
        return

    print("\n[2/3] Parsing CSV...")
    rows = parse_csv(csv_text)
    print(f"   ✅ Parsed {len(rows)} stocks")
    if rows:
        sample = rows[0]
        print(f"   Sample columns: {list(sample.keys())}")
        print(f"   Sample row: {sample}")

    # Validate required columns
    required = {"symbol", "company_name", "industry"}
    found = set(rows[0].keys()) if rows else set()
    missing = required - found
    if missing:
        # Try alternative column names
        alt_names = {
            "symbol":       ["symbol"],
            "company_name": ["company_name", "company", "name"],
            "industry":     ["industry"],
        }
        # Print first row keys so we can debug
        print(f"   ⚠️  Required columns missing: {missing}")
        print(f"   Available columns: {found}")
        return

    print("\n[3/3] Comparing with existing DB...")
    existing = get_existing_symbols()
    print(f"   Existing stocks in DB: {len(existing)}")

    new_symbols     = [r for r in rows if r["symbol"] not in existing]
    existing_in_500 = [r for r in rows if r["symbol"] in existing]
    print(f"   Stocks already in DB:   {len(existing_in_500)}")
    print(f"   New stocks to add:      {len(new_symbols)}")

    # Sector distribution preview
    sector_counts = {}
    for r in new_symbols:
        sec = industry_to_sector(r.get("industry", ""))
        sector_counts[sec] = sector_counts.get(sec, 0) + 1
    print(f"\n   New stocks by sector:")
    for sec, cnt in sorted(sector_counts.items(), key=lambda x: -x[1]):
        print(f"      {sec:<35} {cnt}")

    if not commit:
        print("\n   (dry-run — no DB writes)")
        print("   Re-run with --commit to insert.")
        return

    # Commit: insert new stocks
    print(f"\n[COMMIT] Inserting {len(new_symbols)} new stocks...")
    job_id = start_job_run("EXPAND_TO_NIFTY500", notes=f"{len(new_symbols)} new")

    inserted = 0
    failed = 0
    for r in new_symbols:
        try:
            symbol  = r["symbol"]
            company = r.get("company_name", "")
            isin    = r.get("isin_code", "")
            industry = r.get("industry", "")
            sector   = industry_to_sector(industry)

            upsert_stock(
                symbol=symbol,
                yfinance_ticker=f"{symbol}.NS",
                company_name=company,
                isin=isin,
                is_fno=False,        # explicitly investment universe
                is_active=True,
            )
            upsert_sector_mapping(symbol, [sector])
            inserted += 1
        except Exception as e:
            failed += 1
            print(f"   ❌ {r.get('symbol', '?')}: {e}")

    # Also enrich sector mapping for existing F&O stocks that appear in NIFTY 500
    # but don't update their is_fno flag.
    print(f"\n[Enrichment] Checking existing F&O stocks for sector mapping updates...")
    enriched = 0
    for r in existing_in_500:
        try:
            symbol = r["symbol"]
            industry = r.get("industry", "")
            new_sector = industry_to_sector(industry)
            # Check if this sector is already mapped
            with get_cursor() as (_, cur):
                cur.execute("""
                    SELECT 1 FROM stock_sectors WHERE symbol = %s AND sector = %s
                """, (symbol, new_sector))
                exists = cur.fetchone()
            if not exists:
                with get_cursor() as (_, cur):
                    cur.execute("""
                        INSERT INTO stock_sectors (symbol, sector) VALUES (%s, %s)
                        ON CONFLICT DO NOTHING
                    """, (symbol, new_sector))
                enriched += 1
        except Exception:
            pass

    finish_job_run(
        job_id,
        "SUCCESS" if failed == 0 else "PARTIAL",
        stocks_processed=inserted,
        error_message=f"failed={failed}" if failed else None,
    )

    print("\n" + "=" * 65)
    print("  SUMMARY")
    print("=" * 65)
    print(f"   New stocks inserted:        {inserted}")
    print(f"   Sector mappings enriched:   {enriched}")
    if failed:
        print(f"   Failed:                     {failed}")
    print(f"\n   Next step: python bootstrap_new_stocks.py --commit")
    print(f"   (Bootstraps historical OHLCV for the new stocks only)")


if __name__ == "__main__":
    main()
