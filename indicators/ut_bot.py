"""
ut_bot.py

UT Bot Alert Indicator
Ported from QuantNomad's Pine Script implementation to Python.

Logic:
    1. Compute ATR over `atr_period` candles.
    2. Compute a key value: nLoss = key_value * ATR
    3. Maintain a trailing stop (xATRTrailingStop):
       - If price > prev stop and prev price > prev stop → stop = max(prev stop, price − nLoss)
       - If price < prev stop and prev price < prev stop → stop = min(prev stop, price + nLoss)
       - Otherwise                                        → stop = price − nLoss (uptrend)
                                                                  price + nLoss (downtrend)
    4. Signal:
       - BUY  when close crosses ABOVE trailing stop (from below)
       - SELL when close crosses BELOW trailing stop (from above)
       - NONE otherwise

References:
    https://www.tradingview.com/script/Wd1BU0TS-UT-Bot-Alerts/  (QuantNomad)
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SIGNAL_BUY  = "BUY"
SIGNAL_SELL = "SELL"
SIGNAL_NONE = "NONE"


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_ut_bot(
    df: pd.DataFrame,
    atr_period: int = 1,
    key_value: float = 3.0,
    use_heikin_ashi: bool = True,
) -> pd.DataFrame:
    """
    Compute UT Bot signals on OHLC (or Heikin Ashi) data.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain OHLC columns.
        If ``use_heikin_ashi=True``, also needs HA_Close / HA_High / HA_Low.
        Otherwise plain Close / High / Low are used.
    atr_period : int
        ATR look-back period. QuantNomad's default is 1.
    key_value : float
        Sensitivity multiplier. QuantNomad's default is 3.
    use_heikin_ashi : bool
        Whether to drive the trailing stop from Heikin Ashi closes.

    Returns
    -------
    pd.DataFrame
        Original df plus columns:
            ATR, nLoss, TrailingStop, Signal (BUY / SELL / NONE)
    """

    df = df.copy()

    # ------------------------------------------------------------------
    # Choose price series
    # ------------------------------------------------------------------

    if use_heikin_ashi:
        if "HA_Close" not in df.columns:
            raise ValueError(
                "use_heikin_ashi=True but HA_Close not found. "
                "Run compute_heikin_ashi first or set use_heikin_ashi=False."
            )
        src  = df["HA_Close"].values
        high = df["HA_High"].values  if "HA_High" in df.columns else df["High"].values
        low  = df["HA_Low"].values   if "HA_Low"  in df.columns else df["Low"].values
    else:
        src  = df["Close"].values
        high = df["High"].values
        low  = df["Low"].values

    n = len(src)

    # ------------------------------------------------------------------
    # True Range → ATR  (Wilder's smoothing = EWM with alpha=1/period)
    # ------------------------------------------------------------------

    close = df["Close"].values  # always use raw close for TR calculation

    prev_close = np.empty(n)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]

    tr = np.maximum(
        high - low,
        np.maximum(
            np.abs(high - prev_close),
            np.abs(low  - prev_close),
        ),
    )

    # Wilder ATR (EWM with adjust=False, alpha = 1/period)
    atr_series = (
        pd.Series(tr)
        .ewm(alpha=1.0 / atr_period, adjust=False)
        .mean()
        .values
    )

    n_loss = key_value * atr_series

    # ------------------------------------------------------------------
    # Trailing stop (iterative — cannot be vectorised due to self-reference)
    # ------------------------------------------------------------------

    trailing_stop = np.zeros(n)
    trailing_stop[0] = src[0] - n_loss[0]

    for i in range(1, n):
        prev_stop = trailing_stop[i - 1]
        prev_src  = src[i - 1]
        curr_src  = src[i]
        nl        = n_loss[i]

        if curr_src > prev_stop and prev_src > prev_stop:
            trailing_stop[i] = max(prev_stop, curr_src - nl)
        elif curr_src < prev_stop and prev_src < prev_stop:
            trailing_stop[i] = min(prev_stop, curr_src + nl)
        elif curr_src > prev_stop:
            trailing_stop[i] = curr_src - nl
        else:
            trailing_stop[i] = curr_src + nl

    # ------------------------------------------------------------------
    # Signals  (crossover / crossunder)
    # ------------------------------------------------------------------

    signals = [SIGNAL_NONE] * n

    for i in range(1, n):
        prev_src  = src[i - 1]
        curr_src  = src[i]
        prev_stop = trailing_stop[i - 1]
        curr_stop = trailing_stop[i]

        # BUY: close crosses above trailing stop
        buy  = (prev_src <= prev_stop) and (curr_src > curr_stop)
        # SELL: close crosses below trailing stop
        sell = (prev_src >= prev_stop) and (curr_src < curr_stop)

        if buy:
            signals[i] = SIGNAL_BUY
        elif sell:
            signals[i] = SIGNAL_SELL

    # ------------------------------------------------------------------
    # Assemble result
    # ------------------------------------------------------------------

    df["ATR"]          = atr_series
    df["nLoss"]        = n_loss
    df["TrailingStop"] = trailing_stop
    df["Signal"]       = signals

    return df


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------

def get_latest_signal(df: pd.DataFrame) -> str:
    """Return the signal on the last row ('BUY', 'SELL', or 'NONE')."""

    if "Signal" not in df.columns:
        raise ValueError("DataFrame has no 'Signal' column. Run compute_ut_bot first.")
    return df["Signal"].iloc[-1]


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import yfinance as yf
    from heikin_ashi import append_heikin_ashi  # noqa: E402

    raw = yf.download(
        "RELIANCE.NS",
        period="6mo",
        auto_adjust=False,
        progress=False,
    )

    # Flatten MultiIndex if present
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.droplevel(1)

    ha  = append_heikin_ashi(raw)
    out = compute_ut_bot(ha, atr_period=1, key_value=3, use_heikin_ashi=True)

    print("\n--- Last 10 rows ---")
    print(
        out[["Close", "HA_Close", "ATR", "TrailingStop", "Signal"]].tail(10)
    )

    print(f"\nLatest signal: {get_latest_signal(out)}")
