"""
Apply Corporate Action Adjustments
───────────────────────────────────
Reads pending entries from corporate_action_adjustments table.
For each: multiplies historical OHLC prices (and inverse-adjusts volume)
for all rows BEFORE the effective_date.

Marks adjustment as applied with timestamp + job_id for audit.

Usage:
    python apply_adjustments.py                    # dry-run all pending
    python apply_adjustments.py --commit           # apply all pending
    python apply_adjustments.py VEDL --commit      # apply for specific symbol only
"""

import sys
from datetime import datetime
from db import (
    test_connection,
    get_cursor,
    start_job_run,
    finish_job_run,
)


def list_pending(symbol_filter=None):
    with get_cursor() as (_, cur):
        if symbol_filter:
            cur.execute("""
                SELECT id, symbol, action_type, effective_date,
                       price_factor, volume_factor, notes
                FROM corporate_action_adjustments
                WHERE applied_at IS NULL AND symbol = %s
                ORDER BY effective_date
            """, (symbol_filter,))
        else:
            cur.execute("""
                SELECT id, symbol, action_type, effective_date,
                       price_factor, volume_factor, notes
                FROM corporate_action_adjustments
                WHERE applied_at IS NULL
                ORDER BY effective_date
            """)
        return cur.fetchall()


def _row_count_to_adjust(symbol, effective_date):
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT COUNT(*) FROM daily_prices
            WHERE symbol = %s AND trade_date < %s
        """, (symbol, effective_date))
        return cur.fetchone()[0]


def _sample_before_after(symbol, effective_date):
    """Get the price 1 day before effective_date and on effective_date."""
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT trade_date, close FROM daily_prices
            WHERE symbol = %s AND trade_date < %s
            ORDER BY trade_date DESC LIMIT 1
        """, (symbol, effective_date))
        before = cur.fetchone()

        cur.execute("""
            SELECT trade_date, close FROM daily_prices
            WHERE symbol = %s AND trade_date >= %s
            ORDER BY trade_date ASC LIMIT 1
        """, (symbol, effective_date))
        after = cur.fetchone()
    return before, after


def apply_one(adj_id, symbol, action_type, effective_date,
              price_factor, volume_factor, notes, job_id):
    """Apply a single adjustment. Returns (rows_affected, message)."""
    pf = float(price_factor)
    vf = float(volume_factor) if volume_factor is not None else 1.0

    with get_cursor() as (_, cur):
        cur.execute("""
            UPDATE daily_prices
               SET open   = open   * %s,
                   high   = high   * %s,
                   low    = low    * %s,
                   close  = close  * %s,
                   volume = (volume * %s)::BIGINT,
                   fetched_at = NOW()
             WHERE symbol = %s AND trade_date < %s
        """, (pf, pf, pf, pf, vf, symbol, effective_date))
        rows_affected = cur.rowcount

        cur.execute("""
            UPDATE corporate_action_adjustments
               SET applied_at = NOW(),
                   applied_by_job_id = %s
             WHERE id = %s
        """, (job_id, adj_id))

    return rows_affected


def main():
    args = sys.argv[1:]
    commit = "--commit" in args
    symbol_filter = None
    for a in args:
        if not a.startswith("--"):
            symbol_filter = a.upper()
            break

    print("="*65)
    print("  APPLY CORPORATE ACTION ADJUSTMENTS")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print(f"  Mode: {'COMMIT (will modify data)' if commit else 'DRY-RUN (preview only)'}")
    if symbol_filter:
        print(f"  Filter: symbol = {symbol_filter}")
    print("="*65)

    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return

    pending = list_pending(symbol_filter)
    if not pending:
        print("\n   ✅ No pending adjustments.")
        return

    print(f"\n📋 Found {len(pending)} pending adjustment(s):\n")
    job_id = start_job_run("APPLY_ADJUSTMENTS") if commit else None

    total_rows = 0
    applied = 0
    failed = 0

    for adj in pending:
        adj_id, symbol, action_type, eff_date, pf, vf, notes = adj
        rows_to_adjust = _row_count_to_adjust(symbol, eff_date)
        before, after  = _sample_before_after(symbol, eff_date)

        print(f"   {symbol:<14} | {action_type:<10} | eff: {eff_date} | "
              f"factor: {pf} | rows to adjust: {rows_to_adjust}")
        if notes:
            print(f"      Note: {notes}")
        if before:
            print(f"      Before adj  ({before[0]}): close = ₹{before[1]:.2f}  "
                  f"→ adjusted: ₹{float(before[1]) * float(pf):.2f}")
        if after:
            print(f"      First post-event ({after[0]}): close = ₹{after[1]:.2f}  (unchanged)")

        if commit:
            try:
                affected = apply_one(adj_id, symbol, action_type, eff_date,
                                     pf, vf, notes, job_id)
                total_rows += affected
                applied += 1
                print(f"      ✅ Applied. {affected} rows updated.\n")
            except Exception as e:
                failed += 1
                print(f"      ❌ Failed: {e}\n")
        else:
            print(f"      (dry-run — no changes made)\n")

    if commit:
        finish_job_run(job_id,
                       "SUCCESS" if failed == 0 else "PARTIAL",
                       stocks_processed=applied,
                       error_message=f"failed={failed}" if failed else None)
        print(f"✅ {applied} adjustment(s) applied | {total_rows} total rows updated")
        if failed:
            print(f"❌ {failed} adjustment(s) failed")
    else:
        print(f"📋 Dry-run summary: {len(pending)} adjustment(s) would be applied")
        print(f"   Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
