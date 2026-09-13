"""
crude_oil/signals.py

PCR-based 4-state signal classifier for Crude Oil Mini.

Signal logic
------------
  If buy_confirmed is True:
    current_pcr > avg_last_3 * (1 + threshold/100)  ->  STRONG_BUY
    otherwise                                        ->  RISKY_BUY

  If sell_confirmed is True:
    current_pcr < avg_last_3 * (1 - threshold/100)  ->  STRONG_SELL
    otherwise                                        ->  RISKY_SELL

  If neither:
    NONE

"avg_last_3" is the mean of the 3 PCR records stored in crude_oil_pcr_data
*before* the current reading (i.e. the previous 3 values, newest-first, passed
in as the pcr_history parameter).

This module is intentionally free of any database or network calls so it can be
unit-tested in isolation.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Signal name constants
# ---------------------------------------------------------------------------

SIGNAL_STRONG_BUY  = "STRONG_BUY"
SIGNAL_RISKY_BUY   = "RISKY_BUY"
SIGNAL_STRONG_SELL = "STRONG_SELL"
SIGNAL_RISKY_SELL  = "RISKY_SELL"
SIGNAL_NONE        = "NONE"

# All valid signal values - used for input validation in the API layer
ALL_SIGNALS = (
    SIGNAL_STRONG_BUY,
    SIGNAL_RISKY_BUY,
    SIGNAL_STRONG_SELL,
    SIGNAL_RISKY_SELL,
    SIGNAL_NONE,
)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def calculate_pcr_signal(
    buy_confirmed: bool,
    sell_confirmed: bool,
    current_pcr: Optional[float],
    pcr_history: list[float],
    threshold_pct: float = 2.0,
) -> tuple[str, Optional[float], Optional[float]]:
    """
    Classify the current market state into one of the 4 PCR signals.

    Parameters
    ----------
    buy_confirmed   : True when the UT Bot buy breakout is confirmed on the
                      latest Crude Oil Mini candle.
    sell_confirmed  : True when the UT Bot sell breakout is confirmed.
    current_pcr     : The PCR value that was just recorded / fetched.
    pcr_history     : List of previously stored PCR values, newest first,
                      *excluding* the current reading. Only the first 3
                      elements are used for the average.
    threshold_pct   : Minimum percentage delta vs the 3-value average
                      required to classify as "Strong" instead of "Risky".
                      Default: 2.0% (i.e. >= +2% for Strong Buy, <= -2% for Strong Sell).

    Returns
    -------
    (signal, avg_last_3, delta_pct)
        signal      : one of the SIGNAL_* constants above
        avg_last_3  : mean of the up-to-3 previous PCR values (None if no history)
        delta_pct   : percentage delta of current_pcr vs avg_last_3 (None if
                      avg_last_3 is None or current_pcr is None)
    """
    # No confirmed direction -> no tradeable signal
    if not buy_confirmed and not sell_confirmed:
        return SIGNAL_NONE, None, None

    # Compute average of last 3 stored PCR readings
    valid_prev = [p for p in pcr_history[:3] if p is not None]
    avg_3: Optional[float] = (sum(valid_prev) / len(valid_prev)) if valid_prev else None

    # Percentage delta vs the 3-value average
    delta_pct: Optional[float] = None
    if avg_3 is not None and avg_3 != 0 and current_pcr is not None:
        delta_pct = ((current_pcr - avg_3) / avg_3) * 100

    # Classify
    if buy_confirmed:
        if avg_3 is not None and current_pcr is not None:
            if current_pcr > avg_3 * (1 + threshold_pct / 100):
                return SIGNAL_STRONG_BUY, avg_3, delta_pct
        return SIGNAL_RISKY_BUY, avg_3, delta_pct

    # sell_confirmed must be True here
    if avg_3 is not None and current_pcr is not None:
        if current_pcr < avg_3 * (1 - threshold_pct / 100):
            return SIGNAL_STRONG_SELL, avg_3, delta_pct
    return SIGNAL_RISKY_SELL, avg_3, delta_pct


__all__ = [
    "SIGNAL_STRONG_BUY",
    "SIGNAL_RISKY_BUY",
    "SIGNAL_STRONG_SELL",
    "SIGNAL_RISKY_SELL",
    "SIGNAL_NONE",
    "ALL_SIGNALS",
    "calculate_pcr_signal",
]
