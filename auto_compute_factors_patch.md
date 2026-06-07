# auto_compute_factors.py — Small Update

When `auto_compute_factors.py` successfully writes an adjustment row, it should
ALSO update the parent `corporate_actions` row's `user_decision` from PENDING
to RESOLVED. Otherwise stocks like NMDC keep showing the 🚩 CORP ACTION badge
even after we've handled the event.

---

## The change

In `auto_compute_factors.py`, find `_insert_adjustment()`. Currently it just
inserts into `corporate_action_adjustments`.

Replace with this version that also updates the parent action:

```python
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
```

The added UPDATE statement ensures going forward, every adjustment we create
also clears the PENDING flag on the source corp_actions row.
