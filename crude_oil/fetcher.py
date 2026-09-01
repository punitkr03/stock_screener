"""
crude_oil/fetcher.py

Data ingestion and Upstox API connectors for MCX Crude Oil Mini (CRUDEOILM).
Fetches 5-minute historical candles with Open Interest (OI) and computes Put-Call Ratio (PCR).
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import pandas as pd
import requests

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import UPSTOX_AUTH_TOKEN, CRUDE_OIL_SYMBOL

log = logging.getLogger(__name__)

MCX_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz"
DEFAULT_CRUDE_KEY = "MCX_FO|565900"


def get_auth_headers() -> Dict[str, str]:
    """Return standard headers with Upstox bearer token."""
    token = UPSTOX_AUTH_TOKEN or os.getenv("UPSTOX_AUTH_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def get_active_crude_mini_contract() -> Dict[str, Any]:
    """
    Discover the active near-month CRUDEOILM futures contract from Upstox MCX master.
    Falls back to DEFAULT_CRUDE_KEY if network lookup fails.
    """
    try:
        r = requests.get(MCX_INSTRUMENTS_URL, timeout=30)
        if r.status_code == 200:
            data = json.loads(gzip.decompress(r.content))
            now_ms = datetime.now().timestamp() * 1000
            fut_contracts = [
                d
                for d in data
                if d.get("asset_symbol") == CRUDE_OIL_SYMBOL
                and d.get("instrument_type") in ("FUT", "FUTCOM")
                and d.get("expiry", 0) >= now_ms
            ]
            if fut_contracts:
                fut_contracts.sort(key=lambda x: x.get("expiry", 0))
                active = fut_contracts[0]
                log.info(
                    "Active Crude Mini contract: %s (%s)",
                    active.get("instrument_key"),
                    active.get("trading_symbol"),
                )
                return active
    except Exception as exc:
        log.warning("Could not resolve active contract from Upstox master: %s. Using default.", exc)

    return {
        "instrument_key": DEFAULT_CRUDE_KEY,
        "trading_symbol": f"{CRUDE_OIL_SYMBOL} FUT",
        "asset_symbol": CRUDE_OIL_SYMBOL,
    }


def fetch_5m_candles_chunk(
    instrument_key: str,
    to_date: date,
    from_date: date,
) -> List[List[Any]]:
    """
    Fetch a single chunk (up to 30 days) of 5-minute candles via Upstox v3 API.
    Returns raw candle arrays: [[timestamp, open, high, low, close, volume, oi], ...]
    """
    encoded_key = quote(instrument_key, safe="")
    url = (
        f"https://api.upstox.com/v3/historical-candle/"
        f"{encoded_key}/minutes/5/"
        f"{to_date.isoformat()}/"
        f"{from_date.isoformat()}"
    )

    headers = get_auth_headers()
    r = requests.get(url, headers=headers, timeout=30)

    if r.status_code != 200:
        log.error("Upstox candle API error (%s): %s", r.status_code, r.text[:300])
        return []

    return r.json().get("data", {}).get("candles", [])


def fetch_5m_candles(
    instrument_key: str | None = None,
    days: int = 30,
    end_date: date | None = None,
) -> pd.DataFrame:
    """
    Fetch 5-minute candles for the specified number of days, handling Upstox 30-day window limits.

    Returns DataFrame with columns:
        ['timestamp', 'open', 'high', 'low', 'close', 'volume', 'open_interest', 'symbol', 'instrument_key']
    sorted chronologically (ascending).
    """
    if not instrument_key:
        active = get_active_crude_mini_contract()
        instrument_key = active.get("instrument_key", DEFAULT_CRUDE_KEY)

    to_d = end_date or date.today()
    all_candles: List[List[Any]] = []

    # Upstox API allows max 30-day chunks for intraday historical candles
    CHUNK_DAYS = 25
    remaining_days = max(1, days)
    curr_to = to_d

    while remaining_days > 0:
        span = min(CHUNK_DAYS, remaining_days)
        curr_from = curr_to - timedelta(days=span)
        log.info(
            "Fetching 5m candles for %s from %s to %s",
            instrument_key,
            curr_from,
            curr_to,
        )

        chunk = fetch_5m_candles_chunk(instrument_key, curr_to, curr_from)
        if chunk:
            all_candles.extend(chunk)

        curr_to = curr_from
        remaining_days -= span
        if remaining_days > 0:
            time.sleep(0.1)

    if not all_candles:
        log.warning("No 5-minute candles returned for %s", instrument_key)
        return pd.DataFrame()

    df = pd.DataFrame(
        all_candles,
        columns=["timestamp", "open", "high", "low", "close", "volume", "open_interest"],
    )

    # Convert types and standardize
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    for col in ["volume", "open_interest"]:
        df[col] = df[col].fillna(0).astype(int)

    df["symbol"] = CRUDE_OIL_SYMBOL
    df["instrument_key"] = instrument_key

    # Deduplicate and sort chronologically ascending
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp", ascending=True).reset_index(drop=True)
    return df


def fetch_intraday_5m_candles(instrument_key: str | None = None) -> pd.DataFrame:
    """
    Fetch ONLY today's intraday 5-minute candles via Upstox v3 Intraday API:
    https://api.upstox.com/v3/historical-candle/intraday/{encoded_key}/minutes/5
    """
    if not instrument_key:
        active = get_active_crude_mini_contract()
        instrument_key = active.get("instrument_key", DEFAULT_CRUDE_KEY)

    encoded_key = quote(instrument_key, safe="")
    url = f"https://api.upstox.com/v3/historical-candle/intraday/{encoded_key}/minutes/5"
    headers = get_auth_headers()

    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code != 200:
            log.error("Upstox intraday candle API error (%s): %s", r.status_code, r.text[:300])
            return pd.DataFrame()

        candles = r.json().get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame()

        df = pd.DataFrame(
            candles,
            columns=["timestamp", "open", "high", "low", "close", "volume", "open_interest"],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)
        for col in ["volume", "open_interest"]:
            df[col] = df[col].fillna(0).astype(int)

        df["symbol"] = CRUDE_OIL_SYMBOL
        df["instrument_key"] = instrument_key
        df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp", ascending=True).reset_index(drop=True)
        return df
    except Exception as exc:
        log.error("Failed to fetch intraday 5m candles: %s", exc)
        return pd.DataFrame()



def calculate_pcr(underlying_key: str | None = None) -> Optional[float]:
    """
    Calculate Put-Call Ratio (PCR = Sum of Put OI / Sum of Call OI)
    using Upstox Option Contracts & Quotes API.
    """
    if not underlying_key:
        active = get_active_crude_mini_contract()
        underlying_key = active.get("instrument_key", DEFAULT_CRUDE_KEY)

    headers = get_auth_headers()
    encoded_und = quote(underlying_key, safe="")
    url_opt = f"https://api.upstox.com/v2/option/contract?instrument_key={encoded_und}"

    try:
        r_opt = requests.get(url_opt, headers=headers, timeout=30)
        if r_opt.status_code != 200:
            log.warning("Failed to fetch option contracts (%s): %s", r_opt.status_code, r_opt.text[:200])
            return None

        opt_data = r_opt.json().get("data", [])
        if not opt_data:
            log.warning("No option contracts returned for %s", underlying_key)
            return None

        # Filter for the nearest active expiry
        expiries = sorted(list(set(d.get("expiry") for d in opt_data if d.get("expiry"))))
        if not expiries:
            return None
        near_expiry = expiries[0]

        near_contracts = [d for d in opt_data if d.get("expiry") == near_expiry]
        keys = [d["instrument_key"] for d in near_contracts if d.get("instrument_key")]

        # Query quotes in chunks of 50
        total_ce_oi = 0.0
        total_pe_oi = 0.0

        for i in range(0, len(keys), 50):
            chunk = keys[i : i + 50]
            encoded_chunk = ",".join([quote(k, safe="") for k in chunk])
            url_quotes = f"https://api.upstox.com/v2/market-quote/quotes?instrument_key={encoded_chunk}"

            rq = requests.get(url_quotes, headers=headers, timeout=30)
            if rq.status_code == 200:
                qmap = rq.json().get("data", {})
                for _, quote_data in qmap.items():
                    sym = quote_data.get("symbol", "")
                    oi = float(quote_data.get("oi", 0) or 0)
                    if "CE" in sym or sym.endswith("CE"):
                        total_ce_oi += oi
                    elif "PE" in sym or sym.endswith("PE"):
                        total_pe_oi += oi

        if total_ce_oi > 0:
            pcr = round(total_pe_oi / total_ce_oi, 4)
            log.info("Calculated Crude Oil PCR: %s (PE OI: %s, CE OI: %s)", pcr, total_pe_oi, total_ce_oi)
            return pcr
        elif total_pe_oi > 0:
            return round(total_pe_oi, 4)

    except Exception as exc:
        log.error("Error calculating PCR for %s: %s", underlying_key, exc)

    return None
