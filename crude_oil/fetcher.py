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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
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


def is_crude_oil_market_open(dt: Optional[datetime] = None, grace_minutes: int = 5) -> bool:
    """
    Check if MCX Crude Oil market is currently open for trading / sync.
    Trading hours: Monday to Friday from 09:00 AM to 11:30 PM (23:30) IST.
    A grace_minutes buffer (default: 5 min, up to 23:35 IST) allows finalizing
    and confirming the last 5-minute candle (23:25 - 23:30).
    """
    from datetime import time
    from zoneinfo import ZoneInfo

    if dt is None:
        dt = datetime.now(ZoneInfo("Asia/Kolkata"))
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Kolkata"))
    else:
        dt = dt.astimezone(ZoneInfo("Asia/Kolkata"))

    # Monday = 0, Friday = 4, Saturday = 5, Sunday = 6
    if dt.weekday() > 4:
        return False

    current_time = dt.time()
    market_open = time(9, 0, 0)
    close_min = 30 + grace_minutes
    market_close_with_grace = time(23, min(59, close_min), 0)

    return market_open <= current_time <= market_close_with_grace


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
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
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
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
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


def fetch_current_month_5m_candles(
    instrument_key: str | None = None,
    end_date: date | None = None,
) -> pd.DataFrame:
    """
    Fetch 5-minute candles specifically for the current active calendar month
    (from the 1st of the month to today) for the active futures contract.
    """
    if not instrument_key:
        active = get_active_crude_mini_contract()
        instrument_key = active.get("instrument_key", DEFAULT_CRUDE_KEY)

    to_d = end_date or date.today()
    from_d = to_d.replace(day=1)

    log.info("Fetching current month 5m candles for %s from %s to %s", instrument_key, from_d, to_d)

    # 1. Fetch historical chunk for current month
    raw_month = fetch_5m_candles_chunk(instrument_key, to_d, from_d)

    # 2. Fetch today's live intraday candles
    df_intra = fetch_intraday_5m_candles(instrument_key)

    all_rows = []
    if raw_month:
        all_rows.extend(raw_month)

    df_hist = pd.DataFrame()
    if all_rows:
        df_hist = pd.DataFrame(
            all_rows,
            columns=["timestamp", "open", "high", "low", "close", "volume", "open_interest"],
        )
        df_hist["timestamp"] = pd.to_datetime(df_hist["timestamp"], utc=True)
        for col in ["open", "high", "low", "close"]:
            df_hist[col] = df_hist[col].astype(float)
        for col in ["volume", "open_interest"]:
            df_hist[col] = df_hist[col].fillna(0).astype(int)
        df_hist["symbol"] = CRUDE_OIL_SYMBOL
        df_hist["instrument_key"] = instrument_key

    # Merge month historical chunk with live intraday
    combined = pd.concat([df_hist, df_intra], ignore_index=True) if not df_intra.empty else df_hist

    if combined.empty:
        log.warning("No current month 5m candles returned for %s", instrument_key)
        return pd.DataFrame()

    # Filter strictly to current month
    combined["timestamp"] = pd.to_datetime(combined["timestamp"], utc=True)
    combined = combined[combined["timestamp"].dt.date >= from_d]
    combined["symbol"] = CRUDE_OIL_SYMBOL
    combined["instrument_key"] = instrument_key

    combined = combined.drop_duplicates(subset=["timestamp"]).sort_values("timestamp", ascending=True).reset_index(drop=True)
    log.info("Loaded %s current month candles for %s", len(combined), instrument_key)
    return combined





# In-memory cache for option contract keys of the active expiry
_option_contracts_cache: dict[str, Any] = {
    "symbol": None,
    "underlying_key": None,
    "keys": [],
    "expiry_ms": 0,
    "expiry_date": None,
    "cached_at": 0.0,
}
OPTION_CONTRACTS_CACHE_TTL = 900  # 15 minutes in seconds


def get_active_option_chain(symbol: str = CRUDE_OIL_SYMBOL, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
    """
    Discover the active nearest unexpired option chain for `symbol` from Upstox MCX master.
    Filters unexpired options (expiry >= now_ms), sorts by expiry timestamp ascending,
    and returns the earliest active option chain.

    Returns dict:
        {
            "symbol": str,
            "expiry_ms": int,
            "expiry_date": str (YYYY-MM-DD),
            "underlying_key": str,
            "keys": list[str],
            "count": int,
            "cached_at": float,
        }
    Caches result in memory for OPTION_CONTRACTS_CACHE_TTL (15 min) or until expiry passes.
    """
    global _option_contracts_cache
    now_epoch = time.time()
    now_ms = now_epoch * 1000

    if (
        not force_refresh
        and _option_contracts_cache.get("symbol") == symbol
        and _option_contracts_cache.get("keys")
        and (now_epoch - _option_contracts_cache.get("cached_at", 0.0) < OPTION_CONTRACTS_CACHE_TTL)
        and (_option_contracts_cache.get("expiry_ms", 0) <= 0 or now_ms < _option_contracts_cache["expiry_ms"])
    ):
        return _option_contracts_cache

    try:
        r = requests.get(MCX_INSTRUMENTS_URL, timeout=30)
        if r.status_code == 200:
            data = json.loads(gzip.decompress(r.content))
            # Match symbol in asset_symbol or underlying_symbol
            opts = [
                d
                for d in data
                if (d.get("asset_symbol") == symbol or d.get("underlying_symbol") == symbol)
                and d.get("instrument_type") in ("PE", "CE", "OPT", "OPTFUT", "OPTCOM")
                and d.get("expiry", 0) >= now_ms
            ]

            if not opts:
                fallback_sym = "CRUDEOIL" if symbol == "CRUDEOILM" else "CRUDEOILM"
                opts = [
                    d
                    for d in data
                    if (d.get("asset_symbol") == fallback_sym or d.get("underlying_symbol") == fallback_sym)
                    and d.get("instrument_type") in ("PE", "CE", "OPT", "OPTFUT", "OPTCOM")
                    and d.get("expiry", 0) >= now_ms
                ]

            if opts:
                expiries = sorted(list(set(d.get("expiry") for d in opts if d.get("expiry"))))
                if expiries:
                    near_expiry_ms = expiries[0]
                    chain = [d for d in opts if d.get("expiry") == near_expiry_ms]
                    keys = [d["instrument_key"] for d in chain if d.get("instrument_key")]

                    und_key = None
                    for d in chain:
                        if d.get("underlying_key"):
                            und_key = d["underlying_key"]
                            break
                        elif d.get("asset_key"):
                            und_key = d["asset_key"]
                            break

                    try:
                        expiry_date_str = datetime.fromtimestamp(near_expiry_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    except Exception:
                        expiry_date_str = str(near_expiry_ms)

                    chain_info = {
                        "symbol": symbol,
                        "expiry_ms": near_expiry_ms,
                        "expiry_date": expiry_date_str,
                        "underlying_key": und_key,
                        "keys": keys,
                        "count": len(keys),
                        "cached_at": now_epoch,
                    }
                    _option_contracts_cache = chain_info
                    log.info(
                        "Discovered active option chain for %s: %s strikes, expiry: %s (underlying: %s)",
                        symbol,
                        len(keys),
                        expiry_date_str,
                        und_key,
                    )
                    return chain_info
    except Exception as exc:
        log.error("Could not resolve active option chain for %s from MCX master: %s", symbol, exc)

    if _option_contracts_cache.get("keys"):
        return _option_contracts_cache
    return None


def get_option_contract_keys(underlying_key: str | None = None, force_refresh: bool = False) -> list[str]:
    """
    Fetch and return all option strike instrument keys for the nearest active expiry.
    If underlying_key is provided and returns options from /v2/option/contract, those are used.
    If underlying_key has expired or returns no options, falls back automatically
    to discovering the active unexpired option chain from Upstox MCX master.
    """
    global _option_contracts_cache
    now_epoch = time.time()
    now_ms = now_epoch * 1000

    if (
        not force_refresh
        and underlying_key
        and _option_contracts_cache.get("underlying_key") == underlying_key
        and _option_contracts_cache.get("keys")
        and (now_epoch - _option_contracts_cache.get("cached_at", 0.0) < OPTION_CONTRACTS_CACHE_TTL)
        and (_option_contracts_cache.get("expiry_ms", 0) <= 0 or now_ms < _option_contracts_cache["expiry_ms"])
    ):
        return _option_contracts_cache["keys"]

    if underlying_key:
        headers = get_auth_headers()
        encoded_und = quote(underlying_key, safe="")
        url_opt = f"https://api.upstox.com/v2/option/contract?instrument_key={encoded_und}"

        try:
            r_opt = requests.get(url_opt, headers=headers, timeout=30)
            if r_opt.status_code == 200:
                opt_data = r_opt.json().get("data", [])
                if opt_data:
                    expiries = sorted(list(set(d.get("expiry") for d in opt_data if d.get("expiry"))))
                    if expiries:
                        near_expiry = expiries[0]
                        near_contracts = [d for d in opt_data if d.get("expiry") == near_expiry]
                        keys = [d["instrument_key"] for d in near_contracts if d.get("instrument_key")]
                        if keys:
                            expiry_ms = 0
                            try:
                                exp_dt = datetime.fromisoformat(str(near_expiry))
                                if exp_dt.tzinfo is None:
                                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                                exp_dt = exp_dt.replace(hour=23, minute=59, second=59)
                                expiry_ms = int(exp_dt.timestamp() * 1000)
                            except Exception:
                                expiry_ms = 0

                            _option_contracts_cache = {
                                "symbol": CRUDE_OIL_SYMBOL,
                                "underlying_key": underlying_key,
                                "keys": keys,
                                "expiry_date": str(near_expiry),
                                "expiry_ms": expiry_ms,
                                "cached_at": now_epoch,
                            }
                            log.info("Cached %s option contract keys for expiry %s for underlying %s", len(keys), near_expiry, underlying_key)
                            return keys
                else:
                    log.warning("No active option contracts for %s via option API. Falling back to active chain discovery...", underlying_key)
        except Exception as exc:
            log.warning("Failed to query option contract API for %s: %s. Falling back to active chain discovery...", underlying_key, exc)

    # Fallback to dynamic active option chain discovery
    chain_info = get_active_option_chain(symbol=CRUDE_OIL_SYMBOL, force_refresh=force_refresh)
    if chain_info and chain_info.get("keys"):
        return chain_info["keys"]

    if _option_contracts_cache.get("keys"):
        return _option_contracts_cache["keys"]
    return []


def _fetch_quote_chunk(chunk: List[str], headers: dict) -> tuple[float, float, bool]:
    """Fetch option quotes for a single chunk and return (pe_oi, ce_oi, success)."""
    encoded_chunk = ",".join([quote(k, safe="") for k in chunk])
    url_quotes = f"https://api.upstox.com/v2/market-quote/quotes?instrument_key={encoded_chunk}"
    total_ce = 0.0
    total_pe = 0.0

    for attempt in range(2):
        try:
            rq = requests.get(url_quotes, headers=headers, timeout=10)
            if rq.status_code == 200:
                qmap = rq.json().get("data", {})
                for _, quote_data in qmap.items():
                    sym = quote_data.get("symbol", "")
                    oi = float(quote_data.get("oi", 0) or 0)
                    if "CE" in sym or sym.endswith("CE"):
                        total_ce += oi
                    elif "PE" in sym or sym.endswith("PE"):
                        total_pe += oi
                return total_pe, total_ce, True
            elif rq.status_code == 429:
                time.sleep(0.5)
        except Exception:
            if attempt == 0:
                time.sleep(0.5)

    return 0.0, 0.0, False


def calculate_pcr_details(underlying_key: str | None = None) -> Optional[Dict[str, Any]]:
    """
    Calculate Put-Call Ratio and Open Interest metrics:
        PCR = Sum of Put OI / Sum of Call OI
    using Upstox Option Contracts & Quotes API in parallel.
    Automatically resolves the nearest unexpired option chain if the provided
    underlying_key is expired or has no options.
    Returns dict:
        {
            "timestamp": datetime (UTC),
            "symbol": "CRUDEOILM",
            "instrument_key": underlying_key,
            "expiry_date": str,
            "pcr": float,
            "pe_oi": float,
            "ce_oi": float,
        }
    """
    headers = get_auth_headers()

    try:
        # 1. Resolve active option chain first
        chain_info = get_active_option_chain()
        if chain_info and chain_info.get("keys"):
            keys = chain_info["keys"]
            resolved_underlying = chain_info.get("underlying_key") or underlying_key or DEFAULT_CRUDE_KEY
            expiry_date = chain_info.get("expiry_date")
        else:
            if not underlying_key:
                active_fut = get_active_crude_mini_contract()
                underlying_key = active_fut.get("instrument_key", DEFAULT_CRUDE_KEY)
            keys = get_option_contract_keys(underlying_key)
            resolved_underlying = underlying_key
            expiry_date = None

        if not keys:
            chain_info = get_active_option_chain(force_refresh=True)
            if chain_info and chain_info.get("keys"):
                keys = chain_info["keys"]
                resolved_underlying = chain_info.get("underlying_key") or underlying_key or DEFAULT_CRUDE_KEY
                expiry_date = chain_info.get("expiry_date")
            else:
                log.warning("No option contract keys available for %s", underlying_key or "CRUDEOILM")
                return None

        # Query quotes in chunks of 50 in parallel using ThreadPoolExecutor
        chunks = [keys[i : i + 50] for i in range(0, len(keys), 50)]
        total_ce_oi = 0.0
        total_pe_oi = 0.0
        chunks_succeeded = 0

        with ThreadPoolExecutor(max_workers=min(5, len(chunks) or 1)) as executor:
            futures = [executor.submit(_fetch_quote_chunk, chunk, headers) for chunk in chunks]
            for future in as_completed(futures):
                try:
                    pe_oi, ce_oi, ok = future.result()
                    if ok:
                        chunks_succeeded += 1
                        total_pe_oi += pe_oi
                        total_ce_oi += ce_oi
                except Exception as fut_err:
                    log.warning("Quote chunk fetch error: %s", fut_err)

        if chunks_succeeded == 0:
            log.warning("All option quote chunk requests failed for %s", resolved_underlying)
            return None

        now_utc = datetime.now(timezone.utc)
        if total_ce_oi > 0:
            pcr = round(total_pe_oi / total_ce_oi, 4)
            log.info("Calculated Crude Oil PCR: %s (PE OI: %s, CE OI: %s) for %s (Expiry: %s)", pcr, total_pe_oi, total_ce_oi, resolved_underlying, expiry_date)
            return {
                "timestamp": now_utc,
                "symbol": CRUDE_OIL_SYMBOL,
                "instrument_key": resolved_underlying,
                "expiry_date": expiry_date,
                "pcr": pcr,
                "pe_oi": total_pe_oi,
                "ce_oi": total_ce_oi,
            }
        elif total_pe_oi > 0:
            pcr = round(total_pe_oi, 4)
            return {
                "timestamp": now_utc,
                "symbol": CRUDE_OIL_SYMBOL,
                "instrument_key": resolved_underlying,
                "expiry_date": expiry_date,
                "pcr": pcr,
                "pe_oi": total_pe_oi,
                "ce_oi": total_ce_oi,
            }

    except Exception as exc:
        log.error("Error calculating PCR for %s: %s", underlying_key, exc)

    return None


def calculate_pcr(underlying_key: str | None = None) -> Optional[float]:
    """
    Calculate Put-Call Ratio (PCR = Sum of Put OI / Sum of Call OI).
    Convenience wrapper returning only the float ratio.
    """
    details = calculate_pcr_details(underlying_key=underlying_key)
    return details["pcr"] if details else None
