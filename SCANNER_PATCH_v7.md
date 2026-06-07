# Scanner Patch v7 — Segregate F&O (Trading) vs Non-F&O (Investment)

Once NIFTY 500 stocks are loaded, the scanner will find signals across all 500.
This patch labels each signal with its segment so you can act on them differently.

---

## Edit 1: New helper — fetch is_fno flag in bulk

In `market_scanner.py`, near the top (with other helpers), add:

```python
def get_fno_flag_map():
    """Returns {symbol: is_fno_bool} for all active stocks."""
    from db import get_cursor
    with get_cursor() as (_, cur):
        cur.execute("SELECT symbol, is_fno FROM stocks WHERE is_active = TRUE")
        return {r[0]: bool(r[1]) for r in cur.fetchall()}
```

---

## Edit 2: Load the map at scan start

In `run_full_scan()`, near the top (right after `all_symbols = get_all_stocks(active_only=True)`):

```python
    fno_map = get_fno_flag_map()
```

---

## Edit 3: Attach is_fno to each scan result

Find each place where we set `all_scanned[sym] = {...}` and update them.

Three places typically:

```python
# In sector loop (parallelized):
all_scanned[sym] = r  # may be None
```

Change to:

```python
all_scanned[sym] = r
if r:
    all_scanned[sym]["is_fno"] = fno_map.get(sym, True)
```

And in the sector-attach loop:

```python
all_scanned[sym] = {"sector": sector, **r, "is_fno": fno_map.get(sym, True)}
```

And in remaining-scan parallelized loop:

```python
all_scanned[sym] = {"sector": get_sectors_for_symbol(sym), **r, "is_fno": fno_map.get(sym, True)}
```

---

## Edit 4: Segregate the output

In `format_results()`, find the existing GFS/AGFS display sections:

```python
    if results["gfs_stocks"]:
        lines.append("\n\n🎯 GFS STRATEGY PICKS")
        ...
```

Replace those sections with this segregated version:

```python
    # Segregate GFS / AGFS / Must-Trade by F&O vs Investment
    def _split_by_fno(items):
        fno = [s for s in items if s.get("is_fno", True)]
        inv = [s for s in items if not s.get("is_fno", True)]
        return fno, inv

    gfs_fno,  gfs_inv  = _split_by_fno(results.get("gfs_stocks", []))
    agfs_fno, agfs_inv = _split_by_fno(results.get("agfs_stocks", []))

    # ─── TRADING SECTION (F&O) ───
    lines.append("\n\n" + "=" * 60)
    lines.append("⚡ TRADING CANDIDATES (F&O — leveraged + short-sellable)")
    lines.append("=" * 60)

    if gfs_fno:
        lines.append("\n🎯 GFS PICKS")
        lines.append("-" * 30)
        for stock in gfs_fno:
            lines.append(format_stock_block(stock))
    else:
        lines.append("\n🎯 GFS: None today")

    if agfs_fno:
        lines.append("\n\n⚡ AGFS PICKS")
        lines.append("-" * 30)
        for stock in agfs_fno:
            lines.append(format_stock_block(stock))
    else:
        lines.append("\n\n⚡ AGFS: None today")

    # ─── INVESTMENT SECTION (non-F&O) ───
    lines.append("\n\n" + "=" * 60)
    lines.append("📈 INVESTMENT CANDIDATES (NIFTY 500 ex-F&O — long-only)")
    lines.append("=" * 60)

    if gfs_inv:
        lines.append("\n🎯 GFS PICKS")
        lines.append("-" * 30)
        for stock in gfs_inv:
            lines.append(format_stock_block(stock))
    else:
        lines.append("\n🎯 GFS: None today")

    if agfs_inv:
        lines.append("\n\n⚡ AGFS PICKS")
        lines.append("-" * 30)
        for stock in agfs_inv:
            lines.append(format_stock_block(stock))
    else:
        lines.append("\n\n⚡ AGFS: None today")
```

The Must Trade and Breakout Sector sections at the top can stay as-is since
they're already focused on F&O (driven by bullish sectors which we scan first).

---

## Edit 5 (optional): Show segment in stock block

In `format_stock_block`, find:

```python
def format_stock_block(stock):
    gfs_badge  = " | 🎯 GFS"  if stock.get("gfs")  else ""
    agfs_badge = " | ⚡ AGFS" if stock.get("agfs") else ""
    ca_warn    = " | 🚩 CORP ACTION" if stock.get("corp_action_flag") else ""
```

Add:

```python
def format_stock_block(stock):
    gfs_badge  = " | 🎯 GFS"  if stock.get("gfs")  else ""
    agfs_badge = " | ⚡ AGFS" if stock.get("agfs") else ""
    ca_warn    = " | 🚩 CORP ACTION" if stock.get("corp_action_flag") else ""
    seg_badge  = "" if stock.get("is_fno", True) else " | 📈 INV"
```

Then add `{seg_badge}` to the existing format string:

```python
f"  📌 {stock['ticker']} @ {stock['current_price']}{gfs_badge}{agfs_badge}{ca_warn}{seg_badge}",
```

This way even if you read a stock in the trading section, the `📈 INV` marker (or its absence) tells you what segment it's from.
