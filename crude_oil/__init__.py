"""
crude_oil/__init__.py

Crude Oil Mini (CRUDEOILM) strategy module.
Provides full lifecycle: historical initialization, incremental updates, PCR calculation,
and status querying for frontend and API endpoints.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, Optional

import pandas as pd

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import CRUDE_OIL_INIT_DAYS
from crude_oil.fetcher import calculate_pcr, fetch_5m_candles, fetch_intraday_5m_candles, get_active_crude_mini_contract
from crude_oil.strategy import process_crude_oil_strategy
from crude_oil.db import get_latest_signal_status, load_candles_from_db, save_candles_to_db, init_db

log = logging.getLogger(__name__)


def init_crude_oil_data(days: int = CRUDE_OIL_INIT_DAYS) -> Dict[str, Any]:
    """
    Initialize 1 month of 5-minute candles for Crude Oil Mini, compute
    Heikin Ashi + UT Bot + Breakout confirmation + PCR, and save to DB.
    """
    log.info("Starting initialization of Crude Oil Mini data for %s days...", days)
    init_db()

    contract = get_active_crude_mini_contract()
    instrument_key = contract.get("instrument_key")
    log.info("Using contract: %s (%s)", instrument_key, contract.get("trading_symbol"))

    # 1. Fetch historical 5-minute candles
    df_raw = fetch_5m_candles(instrument_key=instrument_key, days=days)
    if df_raw.empty:
        log.warning("No candles fetched during initialization.")
        return get_latest_signal_status()

    # 2. Calculate PCR from option contracts
    pcr = calculate_pcr(underlying_key=instrument_key)

    # 3. Process strategy (HA, UT Bot, Breakout) and append PCR
    df_processed = process_crude_oil_strategy(df_raw, current_pcr=pcr)

    # 4. Save to database
    save_candles_to_db(df_processed)

    status = get_latest_signal_status()
    log.info(
        "Crude Oil data initialization completed. Total candles: %s, Current Signal: %s, Breakout: %s, PCR: %s",
        status.get("total_candles"),
        status.get("current_signal"),
        status.get("buy_confirmed"),
        status.get("pcr"),
    )
    return status


def update_crude_oil_data(recent_days: int = 1) -> Dict[str, Any]:
    """
    Fast incremental refresh for live polling:
    Fetches only today's intraday 5m candles, checks for new bars or PCR changes,
    and updates the database without re-downloading multi-day historical chunks.
    """
    init_db()

    contract = get_active_crude_mini_contract()
    instrument_key = contract.get("instrument_key")

    # 1. Fetch today's live intraday candles
    recent_raw = fetch_intraday_5m_candles(instrument_key=instrument_key)
    if recent_raw.empty:
        # Fallback to 1-day query if outside market hours or market open gap
        recent_raw = fetch_5m_candles(instrument_key=instrument_key, days=recent_days)

    # 2. Load existing history from DB
    existing_df = load_candles_from_db()

    if existing_df.empty and recent_raw.empty:
        log.warning("No existing or recent candles available to update.")
        return get_latest_signal_status()

    if existing_df.empty:
        combined = recent_raw
    elif recent_raw.empty:
        combined = existing_df
    else:
        # Merge and deduplicate by timestamp
        base_cols = ["timestamp", "open", "high", "low", "close", "volume", "open_interest", "symbol", "instrument_key"]
        r1 = existing_df[[c for c in base_cols if c in existing_df.columns]]
        r2 = recent_raw[[c for c in base_cols if c in recent_raw.columns]]
        combined = pd.concat([r1, r2], ignore_index=True)
        combined = combined.drop_duplicates(subset=["timestamp"]).sort_values("timestamp", ascending=True).reset_index(drop=True)

    # 3. Calculate live PCR
    pcr = calculate_pcr(underlying_key=instrument_key)

    # 4. Recompute strategy on the full time series
    processed = process_crude_oil_strategy(combined, current_pcr=pcr)

    # 5. Save to DB
    save_candles_to_db(processed)

    return get_latest_signal_status()



def get_crude_oil_status(limit: int = 10) -> Dict[str, Any]:
    """Return latest signal, PCR, and breakout status from database for the last N candles."""
    return get_latest_signal_status(limit=limit)



__all__ = [
    "init_crude_oil_data",
    "update_crude_oil_data",
    "get_crude_oil_status",
    "calculate_pcr",
    "process_crude_oil_strategy",
    "fetch_5m_candles",
]
