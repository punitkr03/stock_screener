"""
download_history.py
-------------------
Downloads OHLCV candle history for all NSE equities from the
Upstox Historical Candle API v3 and upserts into the daily_candles table.

Endpoint used:
    GET https://api.upstox.com/v3/historical-candle/{instrument_key}/days/1/{to_date}/{from_date}

instrument_key format for NSE equities: NSE_EQ|{ISIN}  (e.g. NSE_EQ|INE848E01016)

The mapping of trading-symbol → instrument_key is resolved from the Upstox
NSE master instruments file (gzip-compressed JSON), cached locally as
`upstox_instruments.json` and refreshed if older than 24 hours.

Usage:
    python download_history.py                  # full 2-year seed
    python download_history.py --recent         # last 7 calendar days
    python download_history.py --symbol RELIANCE
    python download_history.py --test           # first symbol only
    python download_history.py --period 6mo     # custom window
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote

import pandas as pd
import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from config import (
    DATABASE_URL,
    DOWNLOAD_PERIOD,
    UPSTOX_DELAY_SECS,
    UPSTOX_INSTRUMENTS_CACHE,
    UPSTOX_INSTRUMENTS_URL,
)

load_dotenv()

AUTH_TOKEN = os.environ["UPSTOX_AUTH_TOKEN"]

engine = create_engine(DATABASE_URL)

# ---------------------------------------------------------------------------
# Period → date-range helpers
# ---------------------------------------------------------------------------

_PERIOD_MAP = {
    "1d":   1,
    "2d":   2,
    "3d":   3,
    "5d":   5,
    "7d":   7,
    "1w":   7,
    "2w":   14,
    "1mo":  31,
    "2mo":  62,
    "3mo":  92,
    "6mo":  183,
    "1y":   365,
    "2y":   730,
    "3y":   1095,
    "5y":   1825,
    "10y":  3650,
}


def period_to_dates(period: str) -> tuple[date, date]:
    """
    Translate a shorthand period string (e.g. '2y', '6mo', '5d') to
    (from_date, to_date) where to_date is today.

    Raises ValueError for unrecognised strings.
    """
    key = period.strip().lower()
    if key not in _PERIOD_MAP:
        raise ValueError(
            f"Unknown period '{period}'. "
            f"Supported: {', '.join(_PERIOD_MAP)}"
        )
    to_dt = date.today() + timedelta(days=1)   # +1: Upstox to_date is exclusive
    from_dt = to_dt - timedelta(days=_PERIOD_MAP[key])
    return from_dt, to_dt


# ---------------------------------------------------------------------------
# Upstox instrument-key resolution
# ---------------------------------------------------------------------------

def _cache_is_fresh(path: str, max_age_hours: float = 24.0) -> bool:
    """Return True if the cache file exists and is younger than max_age_hours."""
    if not os.path.exists(path):
        return False
    age = time.time() - os.path.getmtime(path)
    return age < max_age_hours * 3600


def load_instrument_map(force_refresh: bool = False) -> dict[str, str]:
    """
    Return a dict mapping NSE trading_symbol (upper-case) → instrument_key.

    The Upstox NSE master file is downloaded once and cached locally in
    UPSTOX_INSTRUMENTS_CACHE. The cache is refreshed if it is missing,
    older than 24 hours, or force_refresh=True.
    """
    cache_path = UPSTOX_INSTRUMENTS_CACHE

    if not force_refresh and _cache_is_fresh(cache_path):
        print(f"[Instruments] Loading cached map from '{cache_path}' …")
        with open(cache_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    print(f"[Instruments] Downloading master file from Upstox …")
    resp = requests.get(UPSTOX_INSTRUMENTS_URL, timeout=60)
    resp.raise_for_status()

    # The file is gzip-compressed JSON
    with gzip.open(io.BytesIO(resp.content), "rt", encoding="utf-8") as gz:
        instruments: list[dict] = json.load(gz)

    # Filter for NSE equity instruments only (segment = NSE_EQ, instrument_type = EQ)
    instrument_map: dict[str, str] = {}
    for inst in instruments:
        if (
            inst.get("segment") == "NSE_EQ"
            and inst.get("instrument_type") == "EQ"
        ):
            sym = str(inst.get("trading_symbol", "")).strip().upper()
            key = inst.get("instrument_key", "")
            if sym and key:
                instrument_map[sym] = key

    # Persist cache
    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump(instrument_map, fh)

    print(f"[Instruments] {len(instrument_map)} NSE_EQ instruments cached → '{cache_path}'")
    return instrument_map


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def read_symbols() -> pd.DataFrame:
    return pd.read_csv("symbols.csv")


def store_stock_master(df: pd.DataFrame) -> None:
    with engine.begin() as conn:
        for _, row in df.iterrows():
            conn.execute(
                text("""
                    INSERT INTO stocks (symbol, company_name)
                    VALUES (:symbol, :company_name)
                    ON CONFLICT (symbol)
                    DO UPDATE SET
                        company_name = EXCLUDED.company_name,
                        updated_at = NOW()
                """),
                {
                    "symbol": row["SYMBOL"],
                    "company_name": row["NAME"],
                },
            )


def store_history(df: pd.DataFrame) -> None:
    if df.empty:
        return

    with engine.begin() as conn:
        for _, row in df.iterrows():
            conn.exec_driver_sql(
                """
                INSERT INTO daily_candles
                (symbol, candle_date, open, high, low, close, volume)
                VALUES
                (%(symbol)s, %(candle_date)s, %(open)s, %(high)s,
                 %(low)s, %(close)s, %(volume)s)
                ON CONFLICT(symbol, candle_date)
                DO UPDATE SET
                    open   = EXCLUDED.open,
                    high   = EXCLUDED.high,
                    low    = EXCLUDED.low,
                    close  = EXCLUDED.close,
                    volume = EXCLUDED.volume
                WHERE daily_candles.volume = 0 OR EXCLUDED.volume > 0
                """,
                row.to_dict(),
            )


# ---------------------------------------------------------------------------
# Single-symbol Upstox download
# ---------------------------------------------------------------------------

def download_single(
    instrument_key: str,
    symbol: str,
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    """
    Fetch daily OHLCV candles for one instrument from the Upstox
    Historical Candle API v3.

    Returns a tidy DataFrame with columns:
        [symbol, candle_date, open, high, low, close, volume]
    or an empty DataFrame on failure / no data.
    """
    encoded_key = quote(instrument_key, safe="")
    url = (
        f"https://api.upstox.com/v3/historical-candle/"
        f"{encoded_key}/days/1/"
        f"{to_date.isoformat()}/"
        f"{from_date.isoformat()}"
    )

    headers = {
        "Authorization": f"Bearer {AUTH_TOKEN}",
        "Accept": "application/json",
    }

    try:
        r = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        print(f"  [{symbol}] Network error: {exc}")
        return pd.DataFrame()

    if r.status_code != 200:
        print(f"  [{symbol}] API {r.status_code}: {r.text[:120]}")
        return pd.DataFrame()

    try:
        candles = r.json()["data"]["candles"]
    except (KeyError, ValueError) as exc:
        print(f"  [{symbol}] Bad response: {exc}")
        return pd.DataFrame()

    if not candles:
        return pd.DataFrame()

    df = pd.DataFrame(
        candles,
        columns=["candle_date", "open", "high", "low", "close", "volume", "oi"],
    )
    df["symbol"] = symbol
    df["candle_date"] = pd.to_datetime(df["candle_date"]).dt.date

    # Drop zero-volume candles (holiday/weekend fills)
    df = df[df["volume"] > 0]

    return df[["symbol", "candle_date", "open", "high", "low", "close", "volume"]]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download OHLC historical data from Upstox API v3 for NSE equities"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test run: download only the first symbol",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        help="Download history for a single specified symbol (e.g., RELIANCE)",
    )
    parser.add_argument(
        "--recent",
        action="store_true",
        help="Daily incremental mode: download only the last 7 calendar days",
    )
    parser.add_argument(
        "--period",
        type=str,
        default=None,
        help=(
            "Override download period (e.g. 5d, 1mo, 6mo, 2y). "
            f"Defaults to DOWNLOAD_PERIOD in config.py (currently '{DOWNLOAD_PERIOD}')"
        ),
    )
    args = parser.parse_args()

    # Resolve date range
    if args.period:
        from_date, to_date = period_to_dates(args.period)
    elif args.recent:
        from_date, to_date = period_to_dates("7d")
    else:
        from_date, to_date = period_to_dates(DOWNLOAD_PERIOD)

    print(f"Date range: {from_date} → {to_date}")

    # Load symbols
    symbols_df = read_symbols()

    if args.symbol:
        sym_clean = args.symbol.strip().upper().replace(".NS", "")
        symbols_df = symbols_df[symbols_df["SYMBOL"] == sym_clean]
        if symbols_df.empty:
            symbols_df = pd.DataFrame(
                [{"SYMBOL": sym_clean, "NAME": f"{sym_clean} Test", "SECTOR": ""}]
            )
    elif args.test:
        symbols_df = symbols_df.head(1)

    # Upsert stock master
    store_stock_master(symbols_df)

    # Resolve instrument keys
    instrument_map = load_instrument_map()

    symbols_list = symbols_df["SYMBOL"].str.strip().str.upper().tolist()
    total = len(symbols_list)

    missing: list[str] = []
    skipped: list[str] = []
    total_rows = 0

    print(f"\nDownloading {total} symbol(s) via Upstox API …")

    for i, sym in enumerate(symbols_list, start=1):
        inst_key = instrument_map.get(sym)

        if not inst_key:
            print(f"  [{i:4d}/{total}] {sym}: no instrument_key found — skipping")
            missing.append(sym)
            continue

        df = download_single(inst_key, sym, from_date, to_date)

        if df.empty:
            print(f"  [{i:4d}/{total}] {sym}: no candles returned")
            skipped.append(sym)
        else:
            store_history(df)
            print(f"  [{i:4d}/{total}] {sym}: {len(df)} candle(s) stored")
            total_rows += len(df)

        if i < total:
            time.sleep(UPSTOX_DELAY_SECS)

    print(f"\nDone. Total candles stored: {total_rows}")

    if missing:
        print(f"\nNo instrument_key ({len(missing)} symbols — not in Upstox NSE master):")
        for s in missing[:20]:
            print(f"  - {s}")
        if len(missing) > 20:
            print(f"  … and {len(missing) - 20} more")

    if skipped:
        print(f"\nNo candles returned ({len(skipped)} symbols):")
        for s in skipped[:20]:
            print(f"  - {s}")
        if len(skipped) > 20:
            print(f"  … and {len(skipped) - 20} more")


if __name__ == "__main__":
    main()