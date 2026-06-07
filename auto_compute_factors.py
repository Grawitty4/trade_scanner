"""
Auto-Compute Adjustment Factors
─────────────────────────────────
Scans the `corporate_actions` table for DEMERGER events in the last N years.
For each: looks up open price on event date and close on prior trading day in
`daily_prices`, computes factor = open / prev_close, and writes an entry to
`corporate_action_adjustments`.

Usage:
    python auto_compute_factors.py                # last 2 years (default)
    python auto_compute_factors.py --years 5      # last 5 years
    python auto_compute_factors.py --dry-run      # preview without DB writes
"""

import sys
from datetime import datetime, timedelta
from db import test_connection, get_cursor

DEFAULT_LOOKBACK_YEARS = 2
ADJUSTABLE_TYPES = {"DEMERGER", "MERGER"}


def _parse_ex_date(date_str):
    if not date_str:
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except Exception:
            continue
    return None


def _open_on_or_after(symbol, event_date):
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT trade_date, open FROM daily_prices
            WHERE symbol = %s AND trade_date >= %s
            ORDER BY trade_date ASC LIMIT 1
        """, (symbol, event_date))
        return cur.fetchone()


def _close_on_or_before(symbol, event_date):
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT trade_date, close FROM daily_prices
            WHERE symbol = %s AND trade_date < %s
            ORDER BY trade_date DESC LIMIT 1
        """, (symbol, event_date))
        return cur.fetchone()


def _already_have_adjustment(symbol, event_date):
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT 1 FROM corporate_action_adjustments
            WHERE symbol = %s AND effective_date = %s
            LIMIT 1
        """, (symbol, event_date))
        return cur.fetchone() is not None


def _insert_adjustment(symbol, action_type, event_date, factor,
                       open_used, close_used, ex_date_raw, raw_text):
    with get_cursor() as (_, cur):
        # Insert into corporate_action_adjustments
        cur.execute("""
            INSERT INTO corporate_action_adjustments
                (symbol, action_type, effective_date,
                 price_factor, volume_factor, notes)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, effective_date, action_type) DO NOTHING
            RETURNING id
        """, (
            symbol, action_type, event_date,
            float(factor), 1.0,
            f"Auto-computed: open ₹{open_used:.2f} / prev_close ₹{close_used:.2f} "
            f"on {event_date}. NSE ex_date raw='{ex_date_raw}'. "
            f"Original action: {raw_text[:120]}",
        ))
        inserted = cur.fetchone() is not None

        # ALSO update the parent corporate_actions row to RESOLVED
        # so the scanner stops flagging this stock
        cur.execute("""
            UPDATE corporate_actions
            SET user_decision = 'RESOLVED'
            WHERE symbol = %s
              AND action_type = %s
              AND user_decision = 'PENDING'
        """, (symbol, action_type))

        return inserted


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    years = DEFAULT_LOOKBACK_YEARS
    if "--years" in args:
        i = args.index("--years")
        if i + 1 < len(args):
            try: years = int(args[i + 1])
            except: pass

    cutoff = datetime.now().date() - timedelta(days=365 * years)
    print("=" * 65)
    print("  AUTO-COMPUTE ADJUSTMENT FACTORS")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print(f"  Lookback: last {years} year(s) (since {cutoff})")
    print(f"  Mode: {'DRY-RUN (no DB writes)' if dry_run else 'COMMIT'}")
    print("=" * 65)

    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return

    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT DISTINCT symbol, action_type, ex_date, raw_text
            FROM corporate_actions
            WHERE action_type = ANY(%s)
            ORDER BY symbol, ex_date
        """, (list(ADJUSTABLE_TYPES),))
        candidates = cur.fetchall()

    if not candidates:
        print("\n   ⚠️  No DEMERGER/MERGER entries found in corporate_actions table.")
        return

    print(f"\n📋 Candidates: {len(candidates)}\n")

    inserted = 0
    skipped_no_date = 0
    skipped_out_of_window = 0
    skipped_already_exists = 0
    skipped_no_price = 0
    skipped_too_close_to_one = 0
    failures = []

    for symbol, action_type, ex_date_raw, raw_text in candidates:
        ex_date = _parse_ex_date(ex_date_raw)
        if ex_date is None:
            skipped_no_date += 1
            continue
        if ex_date < cutoff:
            skipped_out_of_window += 1
            continue
        if _already_have_adjustment(symbol, ex_date):
            skipped_already_exists += 1
            continue

        prev  = _close_on_or_before(symbol, ex_date)
        after = _open_on_or_after(symbol, ex_date)
        if not prev or not after:
            print(f"   ⚠️  {symbol:<14} {action_type:<10} ex={ex_date}  "
                  f"missing data (prev={bool(prev)}, after={bool(after)})")
            skipped_no_price += 1
            continue

        prev_date, prev_close = prev[0], float(prev[1])
        open_date, open_price = after[0], float(after[1])
        if prev_close <= 0:
            failures.append((symbol, ex_date, "prev_close <= 0"))
            continue

        factor = open_price / prev_close
        if factor >= 0.99:
            print(f"   ⏭️  {symbol:<14} {action_type:<10} ex={ex_date}  "
                  f"factor={factor:.4f} — too close to 1.0 (yfinance likely handled it or no price impact)")
            skipped_too_close_to_one += 1
            continue

        warn = " ⚠️ EXTREME" if factor < 0.05 else ""
        print(f"   ✅ {symbol:<14} {action_type:<10} ex={ex_date}  "
              f"open ₹{open_price:.2f} / prev_close ₹{prev_close:.2f} = "
              f"factor {factor:.4f}{warn}")
        print(f"      ({prev_date} → {open_date})")

        if not dry_run:
            try:
                ok = _insert_adjustment(symbol, action_type, ex_date, factor,
                                        open_price, prev_close, ex_date_raw,
                                        raw_text or "")
                if ok:
                    inserted += 1
            except Exception as e:
                failures.append((symbol, ex_date, str(e)))

    print("\n" + "=" * 65)
    print("  SUMMARY")
    print("=" * 65)
    print(f"  Candidates examined         : {len(candidates)}")
    print(f"  {'Would insert' if dry_run else 'Inserted'}: {inserted}")
    print(f"  Skipped — no usable ex_date : {skipped_no_date}")
    print(f"  Skipped — outside window    : {skipped_out_of_window}")
    print(f"  Skipped — already in table  : {skipped_already_exists}")
    print(f"  Skipped — missing prices    : {skipped_no_price}")
    print(f"  Skipped — factor >= 0.95    : {skipped_too_close_to_one}")
    if failures:
        print(f"  Failures: {len(failures)}")
        for sym, dt, err in failures:
            print(f"     {sym} {dt}: {err}")

    if dry_run:
        print("\n   (dry-run — no DB writes)")
        print("   Re-run without --dry-run to insert.")


if __name__ == "__main__":
    main()
