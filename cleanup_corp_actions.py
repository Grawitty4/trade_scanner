"""
Cleanup Stale Corporate Action Flags
─────────────────────────────────────
Marks corporate_actions entries as RESOLVED when:
  • An adjustment has already been applied for that symbol+date, OR
  • The ex_date is more than 30 days in the past (regardless of adjustment)

This is a one-time cleanup. Going forward, auto_compute_factors.py will
also update user_decision when it creates an adjustment.

Usage:
    python cleanup_corp_actions.py            # dry-run preview
    python cleanup_corp_actions.py --commit   # actually update
"""

import sys
from datetime import datetime, timedelta
from db import test_connection, get_cursor


def _parse_ex_date(date_str):
    if not date_str:
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except Exception:
            continue
    return None


def main():
    commit = "--commit" in sys.argv
    print("=" * 65)
    print("  CORP ACTION FLAG CLEANUP")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print(f"  Mode: {'COMMIT' if commit else 'DRY-RUN'}")
    print("=" * 65)

    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return

    cutoff_date = datetime.now().date() - timedelta(days=30)

    # Gather all PENDING actions
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT id, symbol, action_type, ex_date, raw_text, is_risky, user_decision
            FROM corporate_actions
            WHERE user_decision = 'PENDING'
            ORDER BY symbol, ex_date
        """)
        pending = cur.fetchall()

    if not pending:
        print("\n   ✅ No PENDING corp actions found. Nothing to clean up.")
        return

    print(f"\n📋 Found {len(pending)} PENDING action(s)\n")

    has_adjustment   = []  # adjustment row exists for symbol+date
    stale_no_adjust  = []  # ex_date > 30 days ago, no adjustment
    recent_no_adjust = []  # ex_date within 30 days — leave PENDING

    with get_cursor() as (_, cur):
        for action in pending:
            adj_id, symbol, action_type, ex_date_raw, raw_text, is_risky, decision = action
            ex_dt = _parse_ex_date(ex_date_raw)

            # Check if there's a corresponding adjustment row
            cur.execute("""
                SELECT 1 FROM corporate_action_adjustments
                WHERE symbol = %s
                  AND (effective_date = %s OR (effective_date IS NULL))
                LIMIT 1
            """, (symbol, ex_dt))
            has_adj = cur.fetchone() is not None

            if has_adj:
                has_adjustment.append(action)
            elif ex_dt and ex_dt < cutoff_date:
                stale_no_adjust.append(action)
            else:
                recent_no_adjust.append(action)

    # Reporting
    print(f"   📊 Breakdown:")
    print(f"      ✅ Has adjustment applied   : {len(has_adjustment)}  → will mark RESOLVED")
    print(f"      ⏰ Stale (>30d, no adj)     : {len(stale_no_adjust)}  → will mark RESOLVED")
    print(f"      ⏳ Recent (≤30d, no adj)    : {len(recent_no_adjust)}  → keep PENDING")

    will_update = has_adjustment + stale_no_adjust
    print(f"\n   Will update: {len(will_update)} action(s)")

    if has_adjustment:
        print(f"\n   Sample of stocks with applied adjustments:")
        for a in has_adjustment[:10]:
            print(f"      {a[1]:<14} {a[2]:<10} ex={a[3]}")

    if stale_no_adjust:
        print(f"\n   Sample of stale actions (>30 days old, no adjustment):")
        for a in stale_no_adjust[:10]:
            print(f"      {a[1]:<14} {a[2]:<10} ex={a[3]}")

    if not commit:
        print(f"\n   (dry-run — no DB writes)")
        print(f"   Re-run with --commit to update {len(will_update)} action(s).")
        return

    # Apply updates
    print(f"\n[COMMIT] Updating {len(will_update)} actions...")
    updated_resolved = 0
    updated_stale    = 0

    with get_cursor() as (_, cur):
        for action in has_adjustment:
            cur.execute("""
                UPDATE corporate_actions
                SET user_decision = 'RESOLVED'
                WHERE id = %s
            """, (action[0],))
            if cur.rowcount:
                updated_resolved += 1

        for action in stale_no_adjust:
            cur.execute("""
                UPDATE corporate_actions
                SET user_decision = 'STALE'
                WHERE id = %s
            """, (action[0],))
            if cur.rowcount:
                updated_stale += 1

    print(f"\n   ✅ Marked RESOLVED (had adjustment): {updated_resolved}")
    print(f"   ✅ Marked STALE     (>30 days old) : {updated_stale}")
    print(f"\n   The scanner's 🚩 CORP ACTION badge will no longer appear for these.")


if __name__ == "__main__":
    main()
