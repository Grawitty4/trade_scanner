# Scanner Patch v3 — Pending Adjustment Warning

One small edit to `market_scanner.py` so stocks with pending adjustments
get a ⚠️ marker in the output. This keeps you from acting on garbage data.

---

## Edit 1: New helper to read pending adjustments

Add this function to `market_scanner.py`, near the top (after imports):

```python
def get_pending_adjustment_symbols():
    """Returns set of symbols with at least one unapplied corp action adjustment."""
    from db import get_cursor
    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT DISTINCT symbol FROM corporate_action_adjustments
            WHERE applied_at IS NULL
        """)
        return {r[0] for r in cur.fetchall()}
```

---

## Edit 2: Use it in `run_full_scan()`

Find this block (early in `run_full_scan`):

```python
    flagged     = get_flagged_symbols()
    flagged_set = {f["symbol"] for f in flagged}
    quarantined = get_quarantined_symbols()
    upcoming_7d = get_upcoming_actions(days_ahead=7)
```

Add a line right after it:

```python
    flagged     = get_flagged_symbols()
    flagged_set = {f["symbol"] for f in flagged}
    quarantined = get_quarantined_symbols()
    upcoming_7d = get_upcoming_actions(days_ahead=7)
    pending_adj_set = get_pending_adjustment_symbols()   # ← NEW
```

---

## Edit 3: Pass into scan_stock

Find every call to `scan_stock(sym, flagged_set)` and update the function signature.

In `scan_stock` definition, change:
```python
def scan_stock(symbol, flagged_set=None):
```
To:
```python
def scan_stock(symbol, flagged_set=None, pending_adj_set=None):
```

In the return statement near the end of `scan_stock`, change:
```python
        "corp_action_flag": bool(flagged_set and symbol in flagged_set),
```
To:
```python
        "corp_action_flag": bool(flagged_set and symbol in flagged_set),
        "pending_adjustment": bool(pending_adj_set and symbol in pending_adj_set),
```

Then in `run_full_scan`, update all `scan_stock(sym, flagged_set)` calls to:
```python
scan_stock(sym, flagged_set, pending_adj_set)
```

---

## Edit 4: Show ⚠️ in the output

In `format_stock_block`, find:

```python
    ca_warn    = " | 🚩 CORP ACTION" if stock.get("corp_action_flag") else ""
```

Replace with:

```python
    ca_warn    = " | 🚩 CORP ACTION" if stock.get("corp_action_flag") else ""
    adj_warn   = " | ⚠️ DATA UNADJUSTED" if stock.get("pending_adjustment") else ""
```

Then the line below:

```python
        f"  📌 {stock['ticker']} @ {stock['current_price']}{gfs_badge}{agfs_badge}{ca_warn}",
```

Becomes:

```python
        f"  📌 {stock['ticker']} @ {stock['current_price']}{gfs_badge}{agfs_badge}{ca_warn}{adj_warn}",
```

---

## What this produces

If VEDL has a pending adjustment, you'll see in the report:

```
📌 VEDL @ ₹337.65 | 🚩 CORP ACTION | ⚠️ DATA UNADJUSTED
```

After you run `apply_adjustments.py --commit`, the ⚠️ disappears and VEDL appears with correctly-adjusted RSI.
