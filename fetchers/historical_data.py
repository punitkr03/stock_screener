import os
import sys
import time

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd
import yfinance as yf
from sqlalchemy import create_engine, text

from config import (
    DATABASE_URL,
    DOWNLOAD_PERIOD,
    BATCH_SIZE,
    AUTO_ADJUST,
    SYMBOLS_CSV,
)


engine = create_engine(DATABASE_URL)


def read_symbols(path: str = SYMBOLS_CSV):

    df = pd.read_csv(path)

    return df


def chunk(items, size):

    for i in range(0, len(items), size):
        yield items[i:i + size]


def store_stock_master(df):

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



def flatten_download(df):
    if df.empty:
        return pd.DataFrame()

    if not isinstance(df.columns, pd.MultiIndex):
        return pd.DataFrame()

    rows = []

    tickers = df.columns.get_level_values(0).unique()

    for ticker in tickers:

        try:

            stock = df.xs(
                ticker,
                axis=1,
                level=0,
            ).copy()

        except Exception:
            continue

        stock = stock.dropna()

        # Drop zero-volume candles — these are weekend/holiday fills from yfinance
        # (e.g., Open=High=Low=Close=prev_close, Volume=0) which corrupt HA calculations
        if "Volume" in stock.columns:
            stock = stock[stock["Volume"] > 0]

        if stock.empty:
            continue

        stock = stock.reset_index()

        stock.rename(
            columns={
                "Date": "candle_date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            },
            inplace=True,
        )

        stock["symbol"] = ticker.replace(".NS", "")

        rows.append(
            stock[
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
        )

    if len(rows) == 0:
        return pd.DataFrame()

    return pd.concat(rows)


def store_history(df):

    if df.empty:
        return

    with engine.begin() as conn:

        for _, row in df.iterrows():

            conn.exec_driver_sql(
                """
                INSERT INTO daily_candles

                (
                    symbol,
                    candle_date,
                    open,
                    high,
                    low,
                    close,
                    volume
                )

                VALUES

                (
                    %(symbol)s,
                    %(candle_date)s,
                    %(open)s,
                    %(high)s,
                    %(low)s,
                    %(close)s,
                    %(volume)s
                )

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


import argparse


def main():
    parser = argparse.ArgumentParser(description="Download OHLC historical data from Yahoo Finance")
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
        help="Daily incremental mode: download only the last 5 days instead of the full history period",
    )
    parser.add_argument(
        "--period",
        type=str,
        default=None,
        help="Override download period (e.g. 5d, 1mo, 6mo, 2y). Defaults to DOWNLOAD_PERIOD in config.py",
    )
    args = parser.parse_args()

    # Resolve which period to use
    if args.period:
        period = args.period
    elif args.recent:
        period = "5d"  # just enough to catch today + weekend buffer
    else:
        period = DOWNLOAD_PERIOD  # full history (used for initial seed)

    symbols_df = read_symbols()

    # If a specific symbol is requested, filter the dataframe
    if args.symbol:
        sym_clean = args.symbol.strip().upper().replace(".NS", "")
        symbols_df = symbols_df[symbols_df["SYMBOL"] == sym_clean]
        if symbols_df.empty:
            # If not in symbols.csv, dynamically create a row so it can still be downloaded/tested
            symbols_df = pd.DataFrame([{"SYMBOL": sym_clean, "NAME": f"{sym_clean} Test", "SECTOR": ""}])
    elif args.test:
        # Just grab the first symbol for testing
        symbols_df = symbols_df.head(1)

    store_stock_master(symbols_df)

    symbols = [
        s + ".NS"
        for s in symbols_df["SYMBOL"].tolist()
    ]

    batches = list(
        chunk(
            symbols,
            BATCH_SIZE,
        )
    )

    print(f"Downloading {len(symbols)} stocks... (period={period})")
    print(f"{len(batches)} batches")

    for i, batch in enumerate(batches):

        print(
            f"Batch {i+1}/{len(batches)}"
        )

        data = yf.download(
            tickers=batch,
            period=period,
            interval="1d",
            auto_adjust=AUTO_ADJUST,
            progress=False,
            group_by="ticker",
            threads=True,
        )

        flat = flatten_download(data)

        store_history(flat)

        if i < len(batches) - 1:
            time.sleep(1)
    print("Done")

if __name__ == "__main__":
    main()