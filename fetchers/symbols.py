"""
fetch_symbols.py

Downloads the complete list of active NSE equity symbols from the official
NSE India database and writes them to symbols.csv.

Columns written: SYMBOL, NAME, SECTOR

Usage:
    python fetch_symbols.py
"""

from __future__ import annotations

import io
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd
import requests
from sqlalchemy import create_engine, text
from config import DATABASE_URL, SYMBOLS_CSV

# ---------------------------------------------------------------------------
# NSE public endpoints for all listed equities
# ---------------------------------------------------------------------------

NSE_EQUITY_URLS = [
    # Primary: Official CSV list of all active equities listed on NSE
    "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
]

OUTPUT_FILE = SYMBOLS_CSV

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com",
}


def fetch_from_csv_url(url: str, session: requests.Session) -> pd.DataFrame | None:
    """Attempt to download the NSE CSV file and return a DataFrame."""

    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        df.columns = [c.strip() for c in df.columns]
        return df
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] {url} failed: {exc}")
        return None


def normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure SYMBOL and NAME columns exist; filter by SERIES == 'EQ'."""

    # Normalise whitespace in columns
    df.columns = [c.strip() for c in df.columns]

    # Filter for standard equities ('EQ') only, to filter out mutual funds, debt instruments, ETFs, etc.
    if "SERIES" in df.columns:
        df = df[df["SERIES"].str.strip() == "EQ"].copy()
    elif "series" in df.columns:
        df = df[df["series"].str.strip() == "EQ"].copy()

    col_map = {}
    for col in df.columns:
        upper = col.strip().upper()
        if upper in ("SYMBOL", "TICKER"):
            col_map[col] = "SYMBOL"
        elif upper in ("COMPANY NAME", "NAME", "COMPANYNAME", "SECURITY NAME", "NAME OF COMPANY"):
            col_map[col] = "NAME"

    df = df.rename(columns=col_map)

    if "SYMBOL" not in df.columns:
        raise ValueError(f"Could not find SYMBOL column. Columns: {list(df.columns)}")

    if "NAME" not in df.columns:
        df["NAME"] = df["SYMBOL"]

    df["SECTOR"] = ""  # Full equity list doesn't provide sector mappings

    df["SYMBOL"] = df["SYMBOL"].str.strip()
    df["NAME"]   = df["NAME"].str.strip()

    return df[["SYMBOL", "NAME", "SECTOR"]].drop_duplicates(subset="SYMBOL")


def store_symbols_to_db(df: pd.DataFrame) -> None:
    """Store symbols in PostgreSQL database with ON CONFLICT resolution."""
    print("Storing symbols in database...")
    try:
        engine = create_engine(DATABASE_URL)
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
                        "company_name": row["NAME"]
                    }
                )
        print("✓ Successfully stored symbols in the database.")
    except Exception as exc:
        print(f"[warn] Could not store symbols in database: {exc}")


def main() -> None:

    session = requests.Session()
    session.headers.update(HEADERS)

    df: pd.DataFrame | None = None

    for url in NSE_EQUITY_URLS:
        print(f"Trying: {url}")
        df = fetch_from_csv_url(url, session)

        if df is not None and not df.empty:
            print(f"  ✓ Got {len(df)} rows")
            break

    if df is None or df.empty:
        print(
            "\n[ERROR] Could not fetch equity list from NSE.\n"
            "Please download it manually from:\n"
            "  https://www.nseindia.com/market-data/securities-available-for-trading\n"
            "Save the CSV to symbols.csv in the project root with columns SYMBOL and NAME."
        )
        sys.exit(1)

    df = normalise(df)

    df.to_csv(OUTPUT_FILE, index=False)

    print(f"\nSaved {len(df)} symbols → {OUTPUT_FILE}")
    print(df.head())

    # Save to PostgreSQL
    store_symbols_to_db(df)


if __name__ == "__main__":
    main()


