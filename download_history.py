from __future__ import annotations

import time

import pandas as pd
import yfinance as yf
from sqlalchemy import create_engine, text

from config import DATABASE_URL
from config import DOWNLOAD_PERIOD
from config import BATCH_SIZE
from config import AUTO_ADJUST


engine = create_engine(DATABASE_URL)


def read_symbols():

    df = pd.read_csv("symbols.csv")

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

    #
    # MultiIndex
    #
    # (Open, RELIANCE.NS)
    #

    if df.empty:
        return pd.DataFrame()

    if not isinstance(df.columns, pd.MultiIndex):
        # If it's a single index, this means yfinance returned a flat DataFrame for a single ticker.
        # This can happen if only one ticker is passed or returned.
        # We don't have the ticker name in the columns, so we can't easily map it unless we know it.
        # But since we download in batches of 50, it will be a MultiIndex.
        return pd.DataFrame()

    rows = []

    # Get only the tickers that actually have columns in the DataFrame
    tickers = df.columns.get_level_values(1).unique()

    for ticker in tickers:

        try:

            stock = df.xs(
                ticker,
                axis=1,
                level=1,
            ).copy()

        except Exception:
            continue

        stock = stock.dropna()

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

                DO NOTHING
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
    args = parser.parse_args()

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

    print(f"Downloading {len(symbols)} stocks...")
    print(f"{len(batches)} batches")

    for i, batch in enumerate(batches):

        print(
            f"Batch {i+1}/{len(batches)}"
        )

        data = yf.download(
            tickers=batch,
            period=DOWNLOAD_PERIOD,
            interval="1d",
            auto_adjust=AUTO_ADJUST,
            progress=False,
            group_by="ticker",
            threads=True,
        )

        flat = flatten_download(data)

        store_history(flat)

        if i < len(batches) - 1:
            time.sleep(2)
    print("Done")

if __name__ == "__main__":
    main()