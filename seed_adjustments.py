"""
Seed Known Corporate Action Adjustments
─────────────────────────────────────────
Pre-populates the corporate_action_adjustments table with verified events.

Currently seeded events:
  • VEDL        — Demerger (5 entities) — 2026-04-30 — factor 0.5234
  • HINDUNILVR  — Demerger (Kwality Wall's) — 2025-12-05 — factor 0.97
  • BAJFINANCE  — Stock Split (2:1) + Bonus (4:1) — 2025-06-16 — factor 0.10

Add new events to the SEED list and re-run; existing entries are skipped via
the UNIQUE constraint on (symbol, effective_date, action_type).

Usage:
    python seed_adjustments.py
"""

from datetime import datetime
from db import test_connection, get_cursor


# ─────────────────────────────────────────────
# KNOWN EVENTS (verified from public sources)
# ─────────────────────────────────────────────
# Each tuple: (symbol, action_type, effective_date, price_factor, volume_factor, notes)
#
# price_factor: multiply OLD prices by this. e.g., 0.5234 means old prices become 52.34% of original.
# volume_factor: multiply OLD volumes by this. For splits/bonuses, this is 1/price_factor.
#                For demergers, volume stays as-is (share count unchanged), use 1.0.
#
# Demerger factors derived from cost-apportionment ratios published by each company.

SEED = [
    {
        "symbol":         "VEDL",
        "action_type":    "DEMERGER",
        "effective_date": "2026-04-30",
        "price_factor":   0.5234,
        "volume_factor":  1.0,
        "notes":          "Demerger into 5 entities. Cost apportionment: VEDL retains 52.34%; "
                          "Aluminium 7.15%, Talwandi 12.23%, Malco 21.49%, Iron 6.79%."
    },
    {
        "symbol":         "HINDUNILVR",
        "action_type":    "DEMERGER",
        "effective_date": "2025-12-05",
        "price_factor":   0.97,
        "volume_factor":  1.0,
        "notes":          "Demerger of ice-cream business into Kwality Wall's (India) Ltd. "
                          "Ice-cream business was ~3% of HUL revenue (₹1,800 cr / ~₹60,000 cr). "
                          "Approximate adjustment: 0.97 (refine if exact ratio published)."
    },
    {
        "symbol":         "BAJFINANCE",
        "action_type":    "COMBO",
        "effective_date": "2025-06-16",
        "price_factor":   0.10,
        "volume_factor":  10.0,
        "notes":          "Stock Split 2:1 (FV ₹2 → ₹1) AND Bonus 4:1 on same record date. "
                          "Combined effect: 1 share → 2 shares (split) → 10 shares (bonus on 2). "
                          "Price factor = 1/10 = 0.10. Volume factor = 10x (more shares)."
    },
]


def main():
    print("="*65)
    print("  SEED CORPORATE ACTION ADJUSTMENTS")
    print(f"  {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    print("="*65)

    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return

    inserted, skipped = 0, 0
    for entry in SEED:
        with get_cursor() as (_, cur):
            cur.execute("""
                INSERT INTO corporate_action_adjustments
                    (symbol, action_type, effective_date,
                     price_factor, volume_factor, notes)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, effective_date, action_type) DO NOTHING
                RETURNING id
            """, (entry["symbol"], entry["action_type"], entry["effective_date"],
                  entry["price_factor"], entry["volume_factor"], entry["notes"]))
            row = cur.fetchone()

        if row:
            inserted += 1
            print(f"   ✅ Seeded: {entry['symbol']:<14} | {entry['action_type']:<10} | "
                  f"{entry['effective_date']} | factor={entry['price_factor']}")
        else:
            skipped += 1
            print(f"   ⏭️  Already exists: {entry['symbol']} ({entry['action_type']})")

    print(f"\n   Inserted: {inserted} | Skipped (already present): {skipped}")
    print(f"\n   Next step: python apply_adjustments.py             # preview")
    print(f"              python apply_adjustments.py --commit    # apply")


if __name__ == "__main__":
    main()
