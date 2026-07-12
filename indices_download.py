"""
indices_download.py
--------------------
One-time historical seed for NSE index candles.

Downloads 5 years of daily OHLCV data for every index listed in
nifty_indices.json using the Upstox Historical Candle API, then upserts
the candles into the daily_candles table.

Usage:
    python3 indices_download.py
    python3 indices_download.py --test          # first index only
"""

from __future__ import annotations

import os
import time
import argparse
import json
import requests
from datetime import date, timedelta
from urllib.parse import quote

import pandas as pd
from sqlalchemy import create_engine, text

from dotenv import load_dotenv

from config import DATABASE_URL

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AUTH_TOKEN = os.environ["UPSTOX_AUTH_TOKEN"]

INDICES_JSON = "nifty_indices.json"

TO_DATE = date.today()
FROM_DATE = TO_DATE - timedelta(days=365 * 5)

DELAY_SECS = 0.1       # polite delay between successive API calls

engine = create_engine(DATABASE_URL)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_indices():
    """
    Load the list of NSE index definitions from INDICES_JSON.

    Each entry is expected to look like:
        {
            "instrument_key": "NSE_INDEX|Nifty Bank",
            "name": "Nifty Bank",
            "trading_symbol": "BANKNIFTY",
            ...
        }
    """
    with open(INDICES_JSON, "r") as f:
        return json.load(f)


def ensure_index_in_stocks(symbol: str, name: str) -> None:
    """
    Upsert a row in the stocks master table for this index symbol.
    Uses exchange='NSE_INDEX' to distinguish indices from equities.
    """
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO stocks (symbol, company_name, exchange)
                VALUES (:symbol, :company_name, 'NSE_INDEX')
                ON CONFLICT (symbol)
                DO UPDATE SET
                    company_name = EXCLUDED.company_name,
                    updated_at   = NOW()
            """),
            {"symbol": symbol, "company_name": name},
        )


def download_and_store(instrument_key: str, symbol: str) -> int:
    """
    Download OHLCV candles for a single instrument via the Upstox
    Historical Candle API and upsert into daily_candles.
    Returns the number of rows written.
    """
    encoded_key = quote(instrument_key, safe="")

    url = (
        f"https://api.upstox.com/v3/historical-candle/"
        f"{encoded_key}/days/1/"
        f"{TO_DATE.isoformat()}/"
        f"{FROM_DATE.isoformat()}"
    )

    headers = {
        "Authorization": f"Bearer {AUTH_TOKEN}",
        "Accept": "application/json",
    }

    r = requests.get(url, headers=headers, timeout=30)

    if r.status_code != 200:
        print(f"  API Error {r.status_code}")
        print(r.text)
        return 0

    data = r.json()["data"]["candles"]

    if not data:
        print("  No candles")
        return 0

    df = pd.DataFrame(
        data,
        columns=[
            "candle_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "oi",
        ],
    )

    df["symbol"] = symbol

    df["candle_date"] = pd.to_datetime(df["candle_date"]).dt.date

    df = df[
        [
            "symbol",
            "candle_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]
    ]

    rows = 0

    with engine.begin() as conn:
        for _, row in df.iterrows():
            conn.exec_driver_sql(
                """
                INSERT INTO daily_candles
                (symbol,candle_date,open,high,low,close,volume)
                VALUES
                (%(symbol)s,%(candle_date)s,%(open)s,%(high)s,%(low)s,%(close)s,%(volume)s)
                ON CONFLICT (symbol,candle_date)
                DO UPDATE SET
                    open=EXCLUDED.open,
                    high=EXCLUDED.high,
                    low=EXCLUDED.low,
                    close=EXCLUDED.close,
                    volume=EXCLUDED.volume
                """,
                row.to_dict(),
            )
            rows += 1

    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download 5-year daily OHLCV history for all NSE indices and save to DB."
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test run: process only the first index in the list.",
    )
    args = parser.parse_args()

    indices = load_indices()

    if args.test:
        indices = indices[:1]

    print(f"Downloading {len(indices)} indices...")
    print()

    skipped: list[str] = []
    total_rows = 0

    for i, idx in enumerate(indices, start=1):

        name = idx["name"]
        instrument_key = idx["instrument_key"]
        symbol = idx["trading_symbol"]

        print(f"[{i}/{len(indices)}] {name}")

        ensure_index_in_stocks(symbol, name)

        rows = download_and_store(
            instrument_key=instrument_key,
            symbol=symbol,
        )

        if rows == 0:
            skipped.append(name)

        total_rows += rows
        print(f"  Stored {rows} candles")

        if i != len(indices):
            time.sleep(DELAY_SECS)

    print()
    print(f"Done. Total candles stored: {total_rows}")

    if skipped:
        print(f"\nSkipped ({len(skipped)} indices — no candles returned):")
        for s in skipped:
            print(f"  - {s}")


if __name__ == "__main__":
    main()