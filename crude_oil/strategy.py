"""
crude_oil/strategy.py

Technical strategy engine for Crude Oil Mini (CRUDEOILM).
Computes:
1. Heikin Ashi candles (HA_Open, HA_High, HA_Low, HA_Close)
2. UT Bot Alert Indicator (Wilder's ATR Trailing Stop + BUY/SELL signals)
3. Breakout confirmation (buy_confirmed flag tracking price breaking above buy signal HA high)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

import numpy as np
import pandas as pd

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import CRUDE_OIL_UT_BOT_ATR_PERIOD, CRUDE_OIL_UT_BOT_KEY_VALUE
from indicators.heikin_ashi import append_heikin_ashi
from indicators.ut_bot import SIGNAL_BUY, SIGNAL_NONE, SIGNAL_SELL, compute_ut_bot

log = logging.getLogger(__name__)


def process_crude_oil_strategy(
    df: pd.DataFrame,
    current_pcr: Optional[float] = None,
    atr_period: int = CRUDE_OIL_UT_BOT_ATR_PERIOD,
    key_value: float = CRUDE_OIL_UT_BOT_KEY_VALUE,
) -> pd.DataFrame:

    """
    Process raw OHLCV DataFrame through Heikin Ashi, UT Bot, and Breakout confirmation.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain timestamp, open, high, low, close, volume, open_interest.
    current_pcr : float, optional
        Latest computed Put-Call Ratio to attach to candles.
    atr_period : int
        ATR lookback period (default from config: 55).
    key_value : float
        Sensitivity multiplier for UT Bot (default from config: 1.0).

    Returns
    -------
    pd.DataFrame
        Processed DataFrame with all strategy columns and buy_confirmed boolean flag.
    """
    if df.empty:
        return df

    res = df.copy()

    # Normalize column names to Title Case for indicator functions
    col_mapping = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }
    for lower_c, title_c in col_mapping.items():
        if lower_c in res.columns and title_c not in res.columns:
            res[title_c] = res[lower_c]

    # 1. Compute Heikin Ashi candles
    res = append_heikin_ashi(res)

    # 2. Compute UT Bot signals on Heikin Ashi
    min_needed = atr_period + 1
    if len(res) < min_needed:
        log.warning(
            "Insufficient candle rows (%s) for ATR period %s. Need at least %s.",
            len(res),
            atr_period,
            min_needed,
        )
        res["ATR"] = np.nan
        res["TrailingStop"] = np.nan
        res["Signal"] = SIGNAL_NONE
        res["buy_confirmed"] = False
    else:
        res = compute_ut_bot(
            res,
            atr_period=atr_period,
            key_value=key_value,
            use_heikin_ashi=True,
        )

    # 3. Compute Breakout Confirmation (chronological progression)
    buy_confirmed_list = [False] * len(res)
    is_buy_active = False
    active_buy_high: Optional[float] = None
    has_broken_out = False

    for i in range(len(res)):
        sig = res["Signal"].iloc[i] if "Signal" in res.columns else SIGNAL_NONE
        ha_high = float(res["HA_High"].iloc[i]) if "HA_High" in res.columns else float(res["High"].iloc[i])
        ha_close = float(res["HA_Close"].iloc[i]) if "HA_Close" in res.columns else float(res["Close"].iloc[i])

        if sig == SIGNAL_BUY:
            is_buy_active = True
            active_buy_high = ha_high
            has_broken_out = False
            # Initial signal bar itself is waiting for follow-through breakout
            buy_confirmed_list[i] = False

        elif sig == SIGNAL_SELL:
            is_buy_active = False
            active_buy_high = None
            has_broken_out = False
            buy_confirmed_list[i] = False

        elif is_buy_active and active_buy_high is not None:
            # Check if current candle closes above the buy signal HA High
            if ha_close > active_buy_high:
                has_broken_out = True

            buy_confirmed_list[i] = has_broken_out
        else:
            buy_confirmed_list[i] = False

    res["buy_confirmed"] = buy_confirmed_list

    # 4. Standardize column names for database storage
    db_cols = {
        "timestamp": res["timestamp"] if "timestamp" in res.columns else res.index,
        "symbol": res["symbol"] if "symbol" in res.columns else "CRUDEOILM",
        "instrument_key": res["instrument_key"] if "instrument_key" in res.columns else "",
        "open": res["Open"] if "Open" in res.columns else res["open"],
        "high": res["High"] if "High" in res.columns else res["high"],
        "low": res["Low"] if "Low" in res.columns else res["low"],
        "close": res["Close"] if "Close" in res.columns else res["close"],
        "volume": res["Volume"] if "Volume" in res.columns else res["volume"],
        "open_interest": res["open_interest"] if "open_interest" in res.columns else 0,
        "ha_open": res["HA_Open"] if "HA_Open" in res.columns else None,
        "ha_high": res["HA_High"] if "HA_High" in res.columns else None,
        "ha_low": res["HA_Low"] if "HA_Low" in res.columns else None,
        "ha_close": res["HA_Close"] if "HA_Close" in res.columns else None,
        "atr": res["ATR"] if "ATR" in res.columns else None,
        "trailing_stop": res["TrailingStop"] if "TrailingStop" in res.columns else None,
        "signal": res["Signal"] if "Signal" in res.columns else SIGNAL_NONE,
        "buy_confirmed": res["buy_confirmed"],
    }

    out_df = pd.DataFrame(db_cols)

    # Attach PCR
    if "pcr" in res.columns:
        out_df["pcr"] = res["pcr"]
    else:
        out_df["pcr"] = None

    if current_pcr is not None and not out_df.empty:
        # Populate PCR for latest candles
        out_df.iloc[-1, out_df.columns.get_loc("pcr")] = current_pcr

    return out_df
