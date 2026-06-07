"""
Prune Bad Index Entries
─────────────────────────
Some sector indices have only 1 row (from the failed yfinance fetches).
The scanner falls back to "(synth)" versions automatically, but
keeping these 1-row entries clutters monitoring queries.

This one-time script deletes them.

Usage:
    python prune_bad_indices.py
"""

from db2 import test_connection, get_cursor, delete_index_prices


def main():
    print("Pruning bad index entries...")
    try:
        test_connection()
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return

    # Find indices with fewer than 30 rows AND no "(synth)" in the name
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT index_name, COUNT(*) as rows
            FROM index_prices
            GROUP BY index_name
            HAVING COUNT(*) < 30
        """)
        bad = cur.fetchall()

    if not bad:
        print("   ✅ No bad index entries to prune.")
        return

    print(f"\n   Found {len(bad)} index entries with <30 rows:")
    for name, count in bad:
        print(f"      • {name:<35} ({count} rows)")

    confirm = input("\n   Delete these? [y/N]: ").strip().lower()
    if confirm not in ("y", "yes"):
        print("   Cancelled.")
        return

    total_deleted = 0
    for name, _count in bad:
        n = delete_index_prices(name)
        total_deleted += n
        print(f"   🗑  {name}: {n} rows deleted")

    print(f"\n✅ Deleted {total_deleted} rows total.")
    print("   Scanner will now use '(synth)' versions for these sectors automatically.")


if __name__ == "__main__":
    main()
