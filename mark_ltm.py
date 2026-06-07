"""
Mark LTM with INSUFFICIENT_HISTORY flag.
One-time fix. The scanner will skip stocks with this flag.
"""

from db import get_cursor, test_connection
from datetime import datetime


def main():
    print("Marking LTM with INSUFFICIENT_HISTORY flag...")
    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return

    with get_cursor() as (_, cur):
        cur.execute("""
            UPDATE stocks
            SET data_quality_flag = 'INSUFFICIENT_HISTORY',
                notes = COALESCE(notes,'') || %s
            WHERE symbol = 'LTM'
        """, (f" | Renamed from LTIM on 2026-02-27. yfinance has only post-rename data. "
              f"Re-enable once row_count > 300 (~Aug 2026). Flagged on {datetime.now().date()}.",))
        rowcount = cur.rowcount
    print(f"✅ Updated {rowcount} row(s).")
    print("   The scanner will now skip LTM with a clear message.")


if __name__ == "__main__":
    main()
