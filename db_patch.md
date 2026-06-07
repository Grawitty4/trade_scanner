# db.py Patch — Add `fetch_prices_df_adjusted`

Open `db.py` and find the existing `fetch_prices_df()` function. Right BELOW it,
paste this new function:

```python
def fetch_prices_df_adjusted(symbol, start_date=None, limit=None):
    """
    Like fetch_prices_df but applies all active adjustments in memory.

    Reads:
      - daily_prices (raw OHLCV)
      - corporate_action_adjustments (factors keyed by effective_date)

    Adjustments:
      - For each row in corporate_action_adjustments for this symbol,
        rows in the returned DataFrame with index date < effective_date
        get OHLC multiplied by price_factor and Volume multiplied by volume_factor.

    Returns:
      DataFrame (DatetimeIndex), columns Open/High/Low/Close/Volume, or None.
    """
    df = fetch_prices_df(symbol, start_date=start_date, limit=limit)
    if df is None or df.empty:
        return df

    with get_cursor() as (_, cur):
        cur.execute("""
            SELECT effective_date, price_factor, volume_factor
            FROM corporate_action_adjustments
            WHERE symbol = %s
            ORDER BY effective_date
        """, (symbol,))
        adjustments = cur.fetchall()

    if not adjustments:
        return df

    for eff_date, price_factor, volume_factor in adjustments:
        pf = float(price_factor)
        vf = float(volume_factor) if volume_factor is not None else 1.0
        mask = df.index.date < eff_date
        if not mask.any():
            continue
        df.loc[mask, 'Open']   = df.loc[mask, 'Open']   * pf
        df.loc[mask, 'High']   = df.loc[mask, 'High']   * pf
        df.loc[mask, 'Low']    = df.loc[mask, 'Low']    * pf
        df.loc[mask, 'Close']  = df.loc[mask, 'Close']  * pf
        df.loc[mask, 'Volume'] = df.loc[mask, 'Volume'] * vf

    return df
```

That's the only change. Existing `fetch_prices_df()` stays unchanged for raw-data access.
