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

from datetime import date, datetime, timezone
from config import CRUDE_OIL_INIT_DAYS, CRUDE_OIL_PCR_INTERVAL_SECONDS
from crude_oil.fetcher import (
    calculate_pcr,
    calculate_pcr_details,
    fetch_5m_candles,
    fetch_current_month_5m_candles,
    fetch_intraday_5m_candles,
    get_active_crude_mini_contract,
    is_crude_oil_market_open,
)
from crude_oil.strategy import process_crude_oil_strategy
from crude_oil.db import (
    get_latest_candle_timestamp,
    get_latest_signal_status,
    load_candles_from_db,
    load_pcr_history_from_db,
    save_candles_to_db,
    save_pcr_record_to_db,
    init_db,
    get_active_fcm_tokens,
    get_signal_state,
    save_signal_state,
    save_unconfirmed_signal_state,
    register_fcm_token,
    deregister_fcm_token,
)

log = logging.getLogger(__name__)


def update_crude_oil_pcr(
    force: bool = False,
    min_interval_seconds: int = CRUDE_OIL_PCR_INTERVAL_SECONDS,
    ignore_market_hours: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Calculate live Put-Call Ratio (PCR) for the active Crude Oil contract
    and persist into the dedicated crude_oil_pcr_data table.

    Enforces:
      1. Market hours check: skips calculation and returns latest DB record if market is closed (unless ignore_market_hours=True or force=True).
      2. Strict 2-minute throttling: skips recalculation and returns latest DB record if last update was < min_interval_seconds ago (default: 120s), unless force=True.
    """
    init_db()

    # 1. Market Hours Guard
    if not ignore_market_hours and not force and not is_crude_oil_market_open():
        log.info("MCX Crude Oil market is closed. Skipping PCR calculation.")
        latest_history = load_pcr_history_from_db(limit=1)
        return latest_history[0] if latest_history else None

    # 2. Minimum Interval Throttling Guard
    if not force and min_interval_seconds > 0:
        latest_history = load_pcr_history_from_db(limit=1)
        if latest_history:
            latest_record = latest_history[0]
            ts_val = latest_record.get("timestamp")
            if ts_val:
                if isinstance(ts_val, str):
                    latest_ts = datetime.fromisoformat(ts_val)
                else:
                    latest_ts = ts_val
                if latest_ts.tzinfo is None:
                    latest_ts = latest_ts.replace(tzinfo=timezone.utc)
                else:
                    latest_ts = latest_ts.astimezone(timezone.utc)

                now_utc = datetime.now(timezone.utc)
                elapsed = (now_utc - latest_ts).total_seconds()
                # Use a 5s safety margin to avoid microsecond race conditions
                threshold = max(0, min_interval_seconds - 5)
                if elapsed < threshold:
                    log.debug(
                        "Skipping PCR update: last recorded %.1fs ago (< %ss minimum interval). Returning cached record.",
                        elapsed,
                        min_interval_seconds,
                    )
                    return latest_record

    contract = get_active_crude_mini_contract()
    instrument_key = contract.get("instrument_key")
    pcr_record = calculate_pcr_details(underlying_key=instrument_key)

    if pcr_record:
        save_pcr_record_to_db(pcr_record)
        log.info(
            "Updated Crude Oil PCR: %s (PE OI: %s, CE OI: %s)",
            pcr_record.get("pcr"),
            pcr_record.get("pe_oi"),
            pcr_record.get("ce_oi"),
        )
        # Compute 4-state PCR signal and send FCM notification on state change
        try:
            _check_and_notify_signal(pcr_record)
        except Exception as notify_exc:
            log.error("Error in PCR signal notification: %s", notify_exc, exc_info=True)
    else:
        log.warning("Could not calculate PCR for %s", instrument_key)

    return pcr_record


def init_crude_oil_data(
    start_date: date | None = None,
    days: int | None = None,
) -> Dict[str, Any]:
    """
    Initialize 5-minute candles for Crude Oil Mini for the current active month,
    compute Heikin Ashi + UT Bot (ATR 10 / Key Value 1.0) + Breakout confirmation + PCR,
    and save to PostgreSQL.
    """
    init_db()

    contract = get_active_crude_mini_contract()
    instrument_key = contract.get("instrument_key")
    log.info("Starting Crude Oil Mini initialization for %s (%s)...", instrument_key, contract.get("trading_symbol"))

    # 1. Fetch current month 5-minute candles (or specified days if overridden)
    if days is not None:
        log.info("Fetching custom %s days history...", days)
        df_raw = fetch_5m_candles(instrument_key=instrument_key, days=days)
    else:
        df_raw = fetch_current_month_5m_candles(instrument_key=instrument_key, end_date=start_date)

    if df_raw.empty:
        log.warning("No candles fetched during initialization for %s.", instrument_key)
        return get_latest_signal_status()

    # 2. Calculate and persist PCR to crude_oil_pcr_data
    pcr_details = calculate_pcr_details(underlying_key=instrument_key)
    pcr_val = None
    if pcr_details:
        save_pcr_record_to_db(pcr_details)
        pcr_val = pcr_details.get("pcr")

    # 3. Process strategy (HA, UT Bot 10/1.0, Breakout) and append PCR
    df_processed = process_crude_oil_strategy(df_raw, current_pcr=pcr_val)

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


def update_crude_oil_data() -> Dict[str, Any]:
    """
    Fast incremental refresh for 5-minute candle updates:
    1. Checks if contract rollover or new calendar month occurred.
    2. Fetches today's live intraday 5m candles.
    3. Merges with current month history from DB, attaches latest PCR, and persists.
    """
    init_db()

    contract = get_active_crude_mini_contract()
    instrument_key = contract.get("instrument_key")
    today = date.today()

    # Load existing history from DB for the active contract
    existing_df = load_candles_from_db()

    # Check for rollover: empty DB, different contract key, or month changed
    if existing_df.empty:
        log.info("No candle history found in DB. Initializing current month data...")
        return init_crude_oil_data()

    if "instrument_key" in existing_df.columns and not existing_df["instrument_key"].dropna().empty:
        db_key = existing_df["instrument_key"].dropna().iloc[-1]
        if db_key != instrument_key:
            log.info("Contract rollover detected (DB: %s -> Active: %s). Re-initializing new month...", db_key, instrument_key)
            return init_crude_oil_data()

    latest_ts = existing_df["timestamp"].max() if "timestamp" in existing_df.columns else None
    if latest_ts is not None and hasattr(latest_ts, "month"):
        if latest_ts.month != today.month and latest_ts.year <= today.year:
            log.info("New calendar month detected (%s -> %s). Initializing new month...", latest_ts.month, today.month)
            return init_crude_oil_data()

    # Fetch today's live intraday candles
    recent_raw = fetch_intraday_5m_candles(instrument_key=instrument_key)
    if recent_raw.empty:
        recent_raw = fetch_5m_candles(instrument_key=instrument_key, days=1)

    if existing_df.empty and recent_raw.empty:
        log.warning("No existing or recent candles available to update.")
        return get_latest_signal_status()

    if existing_df.empty:
        combined = recent_raw
    elif recent_raw.empty:
        combined = existing_df
    else:
        # Merge and deduplicate by timestamp, preserving recorded PCR
        base_cols = ["timestamp", "open", "high", "low", "close", "volume", "open_interest", "pcr", "symbol", "instrument_key"]
        r1 = existing_df[[c for c in base_cols if c in existing_df.columns]]
        r2 = recent_raw[[c for c in base_cols if c in recent_raw.columns]]
        combined = pd.concat([r1, r2], ignore_index=True)
        # Keep only current month's candles
        month_start = today.replace(day=1)
        combined["timestamp"] = pd.to_datetime(combined["timestamp"], utc=True)
        combined = combined[combined["timestamp"].dt.date >= month_start]
        combined = combined.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp", ascending=True).reset_index(drop=True)

    # Use latest recorded PCR from DB or calculate fresh
    pcr_hist = load_pcr_history_from_db(limit=1)
    if pcr_hist:
        pcr = pcr_hist[0].get("pcr")
    else:
        pcr = calculate_pcr(underlying_key=instrument_key)

    # Recompute strategy on the current month time series
    processed = process_crude_oil_strategy(combined, current_pcr=pcr)

    # Save to DB
    save_candles_to_db(processed)

    status = get_latest_signal_status()

    # 1. Check for unconfirmed UT Bot BUY/SELL trigger on latest candle
    try:
        _check_and_notify_candle_signal(status)
    except Exception as candle_notif_exc:
        log.error("Error in unconfirmed candle signal notification: %s", candle_notif_exc, exc_info=True)

    # 2. Check for confirmed breakout / PCR state update
    try:
        _check_and_notify_signal(pcr_record=None)
    except Exception as conf_notif_exc:
        log.error("Error in confirmed PCR signal notification: %s", conf_notif_exc, exc_info=True)

    return status


def get_crude_oil_status(limit: int = 10, pcr_limit: int = 50) -> Dict[str, Any]:
    """Return latest signal, PCR history, and breakout status from database."""
    init_db()
    return get_latest_signal_status(limit=limit, pcr_limit=pcr_limit)


# ---------------------------------------------------------------------------
# Private: Telegram & FCM notification dispatch
# ---------------------------------------------------------------------------

def _check_and_notify_candle_signal(status: Dict[str, Any]) -> None:
    """
    Check if the latest completed 5-minute candle produced a UT Bot BUY or SELL
    signal. If so, send Stage 1 unconfirmed Telegram alert (waiting for confirmation).
    Deduplicates against the last recorded unconfirmed signal timestamp in DB.
    """
    from crude_oil.notifications import send_unconfirmed_signal_notification

    latest = status.get("latest_candle")
    if not latest:
        return

    sig = latest.get("signal", "NONE")
    if sig not in ("BUY", "SELL"):
        return

    candle_ts = latest.get("candle_start_time") or latest.get("timestamp")
    if not candle_ts:
        return

    state = get_signal_state()
    last_ts = state.get("last_unconfirmed_ts")

    # If this candle was already notified, skip
    if last_ts and str(last_ts) == str(candle_ts):
        log.debug("Unconfirmed %s signal for candle %s already notified. Skipping.", sig, candle_ts)
        return

    log.info("New unconfirmed %s signal detected on candle %s. Dispatching Telegram alert.", sig, candle_ts)
    res = send_unconfirmed_signal_notification(signal=sig, candle=latest)
    log.info("Unconfirmed signal Telegram alert result: %s", res)

    save_unconfirmed_signal_state(signal=sig, candle_ts=candle_ts)


def _check_and_notify_signal(pcr_record: Optional[Dict[str, Any]] = None) -> None:
    """
    Compute the 4-state PCR signal (STRONG_BUY / RISKY_BUY / STRONG_SELL / RISKY_SELL / NONE).
    If confirmed signal state changes, send Stage 2 Telegram alert (with last 4 PCR readings in IST)
    and FCM push notification.
    """
    from crude_oil.signals import calculate_pcr_signal
    from crude_oil.notifications import send_confirmed_pcr_notification
    from config import PCR_STRONG_SIGNAL_THRESHOLD

    # 1. Get latest candle status
    status = get_latest_signal_status(limit=1, pcr_limit=0)
    buy_confirmed  = status.get("buy_confirmed", False)
    sell_confirmed = status.get("sell_confirmed", False)
    latest_candle  = status.get("latest_candle") or {}

    # 2. Fetch last 4 PCR rows (newest first)
    pcr_hist = load_pcr_history_from_db(limit=4)
    if not pcr_hist:
        log.debug("No PCR history available in DB to compute 4-state signal.")
        return

    current_pcr = pcr_record.get("pcr") if pcr_record else pcr_hist[0].get("pcr")
    # Previous 3 values for average
    if pcr_record:
        prev_pcr_values = [r["pcr"] for r in pcr_hist[1:] if r.get("pcr") is not None]
    else:
        prev_pcr_values = [r["pcr"] for r in pcr_hist[1:4] if r.get("pcr") is not None]

    # 3. Classify 4-state signal
    signal, avg_3, delta_pct = calculate_pcr_signal(
        buy_confirmed=buy_confirmed,
        sell_confirmed=sell_confirmed,
        current_pcr=current_pcr,
        pcr_history=prev_pcr_values,
        threshold_pct=PCR_STRONG_SIGNAL_THRESHOLD,
    )

    # 4. Load previous signal
    prev_state  = get_signal_state()
    prev_signal = prev_state.get("signal", "NONE")

    log.info(
        "PCR Signal: %s (prev: %s) | PCR: %s | Avg-3: %s | Delta%%: %s",
        signal,
        prev_signal,
        current_pcr,
        round(avg_3, 4) if avg_3 is not None else None,
        round(delta_pct, 2) if delta_pct is not None else None,
    )

    # 5. Always persist the latest computed state
    save_signal_state(signal, current_pcr, avg_3, delta_pct)

    # 6. Notify only on state change (e.g., NONE -> STRONG_BUY, RISKY_BUY -> STRONG_BUY, etc.)
    if signal != prev_signal and signal != "NONE":
        log.info(
            "Signal state changed: %s -> %s. Dispatching Telegram & FCM notifications.",
            prev_signal,
            signal,
        )
        tokens = get_active_fcm_tokens()
        result = send_confirmed_pcr_notification(
            signal=signal,
            candle=latest_candle,
            pcr_records=pcr_hist,
            avg_pcr_3=avg_3,
            delta_pct=delta_pct,
            tokens=tokens,
        )
        log.info("Confirmed signal dispatch result: %s", result)
    else:
        log.debug("Signal unchanged (%s) - no notification sent.", signal)


__all__ = [
    "init_crude_oil_data",
    "update_crude_oil_data",
    "update_crude_oil_pcr",
    "get_crude_oil_status",
    "get_latest_candle_timestamp",
    "is_crude_oil_market_open",
    "calculate_pcr",
    "calculate_pcr_details",
    "process_crude_oil_strategy",
    "fetch_5m_candles",
    # FCM / signal / Telegram helpers
    "register_fcm_token",
    "deregister_fcm_token",
    "get_active_fcm_tokens",
    "get_signal_state",
    "save_signal_state",
    "save_unconfirmed_signal_state",
]
