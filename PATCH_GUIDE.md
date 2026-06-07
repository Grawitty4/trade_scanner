# Patch Application Guide

## What to do

1. **Drop `patch_fixes.py` into your project folder**, then run:
   ```bash
   python patch_fixes.py
   ```
   This will:
   - Rename LTIM → LTM (move sector mapping, fetch new history)
   - Rename TIPS → TIPSMUSIC + add TIPSFILMS as new listing
   - Retry the 4 failed indices with alternative tickers
   - Add Nifty India Defence

2. **Apply 3 small edits to existing files** (below) so this doesn't happen again

---

## Edit 1: `bootstrap.py` — use robust fetch for indices

### Find this function (around line 200):
```python
def _fetch_max_history(yf_ticker):
    """Returns DataFrame or None."""
    for attempt in range(3):
        try:
            df = yf.download(yf_ticker, period="max", interval="1d",
                             progress=False, auto_adjust=True)
            if df is None or df.empty:
                return None
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            return df
        except Exception as e:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
            else:
                print(f"      ❌ Failed after 3 attempts: {e}")
                return None
    return None
```

### Replace with:
```python
def _fetch_max_history(yf_ticker):
    """
    Robust history fetch.
    Some Nifty sectoral indices on yfinance reject period='max'.
    We try a cascade: max → start=20y → 10y → 5y.
    """
    from datetime import datetime, timedelta
    attempts = [
        {"period": "max"},
        {"start": (datetime.now() - timedelta(days=20 * 365)).strftime("%Y-%m-%d")},
        {"start": (datetime.now() - timedelta(days=10 * 365)).strftime("%Y-%m-%d")},
        {"period": "5y"},
        {"period": "1y"},
    ]
    last_err = None
    for kwargs in attempts:
        for retry in range(2):  # 2 retries per kwarg
            try:
                df = yf.download(yf_ticker, interval="1d",
                                 progress=False, auto_adjust=True, **kwargs)
                if df is None or df.empty:
                    break  # try next kwargs
                df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
                return df
            except Exception as e:
                last_err = str(e)
                if retry == 0:
                    time.sleep(2)
                else:
                    break  # try next kwargs
    if last_err:
        print(f"      ❌ All attempts failed: {last_err}")
    return None
```

---

## Edit 2: `bootstrap.py` — update sector index list

### Find `NSE_SECTOR_INDICES` (top of file) and **replace these lines**:

OLD:
```python
"Nifty Healthcare":         "NIFTY_HEALTHCARE.NS",
"Nifty Chemicals":          "NIFTY_CHEM.NS",
"Nifty Consumer Durables":  "NIFTY_CONSR_DURBL.NS",
"Nifty Oil & Gas":          "NIFTY_OIL_AND_GAS.NS",
```

NEW:
```python
"Nifty Healthcare":         "^CNXHC",
"Nifty Chemicals":          "^CNXCHEM",
"Nifty Consumer Durables":  "^CNXCONSDUR",
"Nifty Oil & Gas":          "^CNXOILGAS",
"Nifty India Defence":      "NIFTY_IND_DEFENCE.NS",
```

---

## Edit 3: `market_scanner.py` — same NSE_SECTOR_INDICES update

Apply the **exact same change** to the `NSE_SECTOR_INDICES` dict in `market_scanner.py`.

Also update `SECTOR_STOCKS` mapping:

### Find `"Nifty IT"` list and replace `LTIM` with `LTM`:
OLD:
```python
"Nifty IT": [
    "TCS", "INFY", "WIPRO", "HCLTECH", "TECHM",
    "LTIM", "MPHASIS", "PERSISTENT", "COFORGE", "OFSS"
],
```

NEW:
```python
"Nifty IT": [
    "TCS", "INFY", "WIPRO", "HCLTECH", "TECHM",
    "LTM", "MPHASIS", "PERSISTENT", "COFORGE", "OFSS"
],
```

### Find `"Nifty Media"` list and replace `TIPS` with `TIPSMUSIC` + add `TIPSFILMS`:
OLD:
```python
"Nifty Media": [
    "ZEEL", "SUNTV", "PVRINOX", "NAZARA", "SAREGAMA", "TIPS"
],
```

NEW:
```python
"Nifty Media": [
    "ZEEL", "SUNTV", "PVRINOX", "NAZARA", "SAREGAMA", "TIPSMUSIC", "TIPSFILMS"
],
```

### Add new Nifty India Defence entry (optional — add stocks you want tracked):
```python
"Nifty India Defence": [
    "HAL", "BEL", "BDL", "MAZDOCK", "COCHINSHIP",
    "GRSE", "MTARTECH", "DATAPATTNS", "BEML"
],
```

---

## Same edits in `bootstrap.py`

Apply the same `SECTOR_STOCKS` updates to `bootstrap.py` (it has its own copy of the dict).

---

## After applying the edits

```bash
# 1. Apply DB-level patch
python patch_fixes.py

# 2. Verify with scanner
python market_scanner.py
```

Check that:
- LTM, TIPSMUSIC, TIPSFILMS appear in DB
- Missing indices now have data
- Scanner output doesn't show "Data Unavailable" for the 4 indices
