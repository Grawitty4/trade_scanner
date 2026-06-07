# Scanner Edits for This Round

Two small edits to `market_scanner.py`. Apply both before testing.

---

## Edit 1: Skip stocks flagged INSUFFICIENT_HISTORY

### Find this function (around line 415):

```python
def scan_stock(symbol, flagged_set=None):
    daily, weekly, monthly = load_stock_data(symbol)
    if daily is None:
        return None
```

### Replace with:

```python
def _is_skipped_by_quality_flag(symbol):
    """Returns reason string if stock should be skipped, else None."""
    from db import get_cursor
    with get_cursor() as (_, cur):
        cur.execute("SELECT data_quality_flag FROM stocks WHERE symbol = %s",
                    (symbol,))
        row = cur.fetchone()
    if row and row[0] and row[0] != 'OK':
        return row[0]
    return None


def scan_stock(symbol, flagged_set=None):
    skip_reason = _is_skipped_by_quality_flag(symbol)
    if skip_reason:
        return None  # silently skip — already known issue
    daily, weekly, monthly = load_stock_data(symbol)
    if daily is None:
        return None
```

---

## Edit 2: Prefer real index, fall back to synthesized

### Find this function (around line 530):

```python
def scan_sector_direction():
    out = {}
    for sector in NSE_SECTOR_INDICES.keys():
        df = fetch_index_df(sector)
        if df is None or len(df) < 30:
            out[sector] = {"status": "Data Unavailable", "rsi": 0}
            continue
        close   = df['Close']
```

### Replace with:

```python
def scan_sector_direction():
    """
    For each sector: try the real (yfinance-sourced) index first.
    If unavailable, fall back to the synthesized "<Sector> (synth)" index.
    """
    out = {}
    for sector in NSE_SECTOR_INDICES.keys():
        df = fetch_index_df(sector)
        source = "real"

        # Fallback to synthesized
        if df is None or len(df) < 30:
            df = fetch_index_df(sector + " (synth)")
            source = "synth"

        if df is None or len(df) < 30:
            out[sector] = {"status": "Data Unavailable", "rsi": 0, "source": "none"}
            continue

        close   = df['Close']
```

### Then at the bottom of that loop (where you build the result dict), find:

```python
        out[sector] = {"status": status, "rsi": round(rsi, 2)}
```

### Replace with:

```python
        out[sector] = {"status": status, "rsi": round(rsi, 2), "source": source}
```

### Lastly, update the formatter to show the source. Find:

```python
    lines.append("\n📂 SECTOR STATUS")
    lines.append("-" * 30)
    for sector, data in results["sectors"].items():
        if isinstance(data, dict):
            status = data.get("status", "Unknown")
            emoji = {"Bullish Breakout": "🟢", "Neutral": "🟡", "Bearish": "🔴"}.get(status, "⚪")
            lines.append(f"  {emoji} {sector}: {status}  (RSI: {data.get('rsi', 'N/A')})")
```

### Replace with:

```python
    lines.append("\n📂 SECTOR STATUS")
    lines.append("-" * 30)
    for sector, data in results["sectors"].items():
        if isinstance(data, dict):
            status = data.get("status", "Unknown")
            emoji = {"Bullish Breakout": "🟢", "Neutral": "🟡", "Bearish": "🔴"}.get(status, "⚪")
            src   = data.get("source", "?")
            src_marker = "" if src == "real" else f" [{src}]"
            lines.append(f"  {emoji} {sector}{src_marker}: {status}  (RSI: {data.get('rsi', 'N/A')})")
```

This way, synthesized sectors are visually marked `[synth]` in the output so you can tell them apart.
