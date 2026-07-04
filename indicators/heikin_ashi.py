"""
heikin_ashi.py

Utility functions for generating Heikin Ashi candles.

Author: Punit's NSE Scanner
"""

from __future__ import annotations

import pandas as pd


REQUIRED_COLUMNS = [
    "Open",
    "High",
    "Low",
    "Close",
]


def validate_dataframe(df: pd.DataFrame) -> None:
    """
    Ensure the dataframe contains OHLC columns.
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]

    if missing:
        raise ValueError(
            f"Missing required columns: {missing}"
        )


def compute_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute Heikin Ashi candles.

    Parameters
    ----------
    df : pandas.DataFrame

    Expected columns:

        Open
        High
        Low
        Close

    Returns
    -------
    DataFrame

    Columns:

        HA_Open
        HA_High
        HA_Low
        HA_Close
    """

    validate_dataframe(df)

    ha = pd.DataFrame(index=df.index)

    #
    # HA Close
    #

    ha["HA_Close"] = (
        df["Open"]
        + df["High"]
        + df["Low"]
        + df["Close"]
    ) / 4.0

    #
    # HA Open
    #

    ha_open = [0.0] * len(df)

    #
    # First candle
    #

    ha_open[0] = (
        df["Open"].iloc[0]
        + df["Close"].iloc[0]
    ) / 2.0

    #
    # Remaining candles
    #

    for i in range(1, len(df)):
        ha_open[i] = (
            ha_open[i - 1]
            + ha["HA_Close"].iloc[i - 1]
        ) / 2.0

    ha["HA_Open"] = ha_open

    #
    # HA High
    #

    ha["HA_High"] = pd.concat(
        [
            df["High"],
            ha["HA_Open"],
            ha["HA_Close"],
        ],
        axis=1,
    ).max(axis=1)

    #
    # HA Low
    #

    ha["HA_Low"] = pd.concat(
        [
            df["Low"],
            ha["HA_Open"],
            ha["HA_Close"],
        ],
        axis=1,
    ).min(axis=1)

    return ha


def append_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """
    Returns original dataframe with
    HA columns appended.
    """

    ha = compute_heikin_ashi(df)

    return pd.concat(
        [
            df.copy(),
            ha,
        ],
        axis=1,
    )


if __name__ == "__main__":
    import yfinance as yf

    df = yf.download(
        "RELIANCE.NS",
        period="3mo",
        auto_adjust=False,
        progress=False,
    )

    df = append_heikin_ashi(df)

    print(
        df[
            [
                "Open",
                "High",
                "Low",
                "Close",
                "HA_Open",
                "HA_High",
                "HA_Low",
                "HA_Close",
            ]
        ].tail()
    )