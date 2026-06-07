"""
Revert an Applied Adjustment
─────────────────────────────
Multiplies prices/volumes by the INVERSE of an applied adjustment,
restoring the original raw values from yfinance.

After reverting, the adjustment row stays in the table with applied_at=NULL,
so you can update the factor and re-run apply_adjustments.py.

Usage:
    python revert_adjustment.py VEDL                    # dry-run for VEDL
    python revert_adjustment.py VEDL --commit           # actually revert
    python revert_adjustment.py VEDL 2026-04-30 --commit  # specific effective_date
"""

import sys
from datetime import datetime
from db import (
    test_connection,
    get_cursor,
    start_job_run,
    finish_job_run,
)


def find_adjustment(symbol, effective_date=None):
    with get_cursor() as (_, cur):
        if effective_date:
            cur.execute("""
                SELECT id, symbol, action_type, effective_date,
                       price_factor, volume_factor, applied_at, notes
                FROM corporate_action_adjustments
                WHERE symbol = %s AND effective_date = %s
                ORDER BY effective_date
            """, (symbol, effective_date))
        else:
            cur.execute("""
                SELECT id, symbol, action_type, effective_date,
                       price_factor, volume_factor, applied_at, notes
                FROM corporate_action_adjustments
                WHERE symbol = %s
                ORDER BY effective_date
            """, (symbol,))
        return cur.fetchall()


def revert_one(adj_id, symbol, effective_date, price_factor,
               volume_factor, commit, job_id):
    """Reverse the multiplication. Returns rows_affected."""
    inv_pf = 1.0 / float(price_factor)
    inv_vf = 1.0 / float(volume_factor) if volume_factor and float(volume_factor) != 0 else 1.0

    if not commit:
        # Just count what would change
        with get_cursor() as (_, cur):
            cur.execute("""
                SELECT COUNT(*) FROM daily_prices
                WHERE symbol = %s AND trade_date < %s
            """, (symbol, effective_date))
            return cur.fetchone()[0]

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
        """, (inv_pf, inv_pf, inv_pf, inv_pf, inv_vf, symbol, effective_date))
        rows_affected = cur.rowcount

        # Mark as unapplied so it can be re-applied later with a corrected factor
        cur.execute("""
            UPDATE corporate_action_adjustments
            SET applied_at = NULL,
                applied_by_job_id = NULL,
                notes = COALESCE(notes,'') || %s
            WHERE id = %s
        """, (f" | Reverted on {datetime.now().date()} for correction", adj_id))

    return rows_affected


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    commit = "--commit" in args
    positional = [a for a in args if not a.startswith("--")]
    if not positional:
        print("❌ Specify a symbol.")
        return

    symbol = positional[0].upper()
    effective_date = positional[1] if len(positional) >= 2 else None

    print("="*65)
    print("  REVERT CORPORATE ACTION ADJUSTMENT")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print(f"  Mode: {'COMMIT' if commit else 'DRY-RUN'} | Symbol: {symbol}"
          + (f" | Date: {effective_date}" if effective_date else ""))
    print("="*65)

    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return

    matches = find_adjustment(symbol, effective_date)
    if not matches:
        print(f"\n   No adjustments found for {symbol}"
              + (f" on {effective_date}" if effective_date else "") + ".")
        return

    print(f"\n📋 Found {len(matches)} adjustment(s):\n")

    job_id = start_job_run("REVERT_ADJUSTMENT") if commit else None
    reverted = 0
    rows_total = 0

    for adj in matches:
        adj_id, sym, action_type, eff_date, pf, vf, applied_at, notes = adj
        status = "APPLIED" if applied_at else "NOT YET APPLIED"
        print(f"   {sym:<12} | {action_type:<10} | eff: {eff_date} | "
              f"factor: {pf} | status: {status}")
        if notes:
            print(f"      Note: {notes[:120]}")

        if not applied_at:
            print(f"      ⏭️  Skipped — never applied, nothing to revert\n")
            continue

        rows = revert_one(adj_id, sym, eff_date, pf, vf, commit, job_id)
        if commit:
            print(f"      ✅ Reverted. {rows} rows restored to raw values.")
            print(f"      ℹ️  Row is now marked unapplied — update factor & re-apply.\n")
            reverted += 1
            rows_total += rows
        else:
            print(f"      (dry-run) Would revert {rows} rows\n")

    if commit:
        finish_job_run(job_id, "SUCCESS",
                       stocks_processed=reverted)
        print(f"✅ Reverted {reverted} adjustment(s) | {rows_total} rows restored")
        print(f"\n   Next steps:")
        print(f"   1. Edit seed_adjustments.py with the corrected factor")
        print(f"   2. Update the row directly OR re-seed:")
        print(f"      UPDATE corporate_action_adjustments")
        print(f"      SET price_factor = <correct value>")
        print(f"      WHERE symbol = '{symbol}' AND applied_at IS NULL;")
        print(f"   3. Run: python apply_adjustments.py --commit")


if __name__ == "__main__":
    main()
