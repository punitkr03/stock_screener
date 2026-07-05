"""
scanner.py

Daily NSE UT Bot Scanner
========================

Workflow:
    1. Load all active symbols from PostgreSQL (falls back to symbols.csv).
    2. For each symbol, fetch OHLC from daily_candles.
    3. Compute Heikin Ashi candles.
    4. Run UT Bot.
    5. Extract today's signal (last row).
    6. Persist signals to:
       - scan_results table (full history)
       - latest_buy_signal table (rolling last BUY)
    7. Write today's BUY/SELL signals to signals_YYYY-MM-DD.csv.

Usage:
    python scanner.py [--date YYYY-MM-DD] [--csv-only] [--no-db]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, text

# ---------------------------------------------------------------------------
# Project-local imports
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "indicators"))

from config import (
    DATABASE_URL,
    UT_BOT_ATR_PERIOD,
    UT_BOT_KEY_VALUE,
)
from indicators.heikin_ashi import append_heikin_ashi
from indicators.ut_bot import SIGNAL_BUY, SIGNAL_NONE, SIGNAL_SELL, compute_ut_bot

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_CANDLES = max(30, UT_BOT_ATR_PERIOD + 1)  # minimum rows needed to compute meaningful ATR

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def get_engine():
    return create_engine(DATABASE_URL)


def load_symbols_from_db(engine) -> list[str]:
    """Return list of active symbols from the stocks table."""

    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT symbol FROM stocks WHERE is_active = TRUE ORDER BY symbol")
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not load symbols from DB: %s. Falling back to CSV.", exc)
        return []


def load_symbols_from_csv(path: str = "symbols.csv") -> list[str]:
    """Fallback: read symbols from CSV."""

    df = pd.read_csv(path)
    return df["SYMBOL"].str.strip().tolist()


def load_ohlc_from_db(engine, symbol: str, limit: int = 200) -> pd.DataFrame:
    """
    Fetch the most recent `limit` daily candles for a symbol.

    Returns a DataFrame with columns: Date (index), Open, High, Low, Close, Volume.
    """

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT candle_date, open, high, low, close, volume
                FROM   daily_candles
                WHERE  symbol = :sym
                ORDER  BY candle_date DESC
                LIMIT  :lim
                """
            ),
            {"sym": symbol, "lim": limit},
        ).fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").set_index("Date")
    return df


def upsert_latest_buy(conn, symbol: str, signal_date: date, row: pd.Series) -> None:
    """Insert or update the latest_buy_signal table."""

    conn.execute(
        text(
            """
            INSERT INTO latest_buy_signal
                (symbol, signal_date, signal_open, signal_high, signal_low, signal_close)
            VALUES
                (:sym, :sd, :so, :sh, :sl, :sc)
            ON CONFLICT (symbol)
            DO UPDATE SET
                signal_date  = EXCLUDED.signal_date,
                signal_open  = EXCLUDED.signal_open,
                signal_high  = EXCLUDED.signal_high,
                signal_low   = EXCLUDED.signal_low,
                signal_close = EXCLUDED.signal_close,
                breakout     = FALSE,
                updated_at   = NOW()
            """
        ),
        {
            "sym": symbol,
            "sd":  signal_date,
            "so":  float(row["Open"]),
            "sh":  float(row["High"]),
            "sl":  float(row["Low"]),
            "sc":  float(row["Close"]),
        },
    )


def delete_latest_buy(conn, symbol: str) -> None:
    """Remove the active BUY signal and both watch list entries on SELL signal."""
    conn.execute(
        text("DELETE FROM latest_buy_signal WHERE symbol = :sym"),
        {"sym": symbol},
    )
    conn.execute(
        text("DELETE FROM buy_watch_list WHERE symbol = :sym"),
        {"sym": symbol},
    )
    conn.execute(
        text("DELETE FROM confirmed_breakouts WHERE symbol = :sym"),
        {"sym": symbol},
    )


def upsert_buy_watch_list(conn, r: dict) -> None:
    """Insert a BUY signal into buy_watch_list. Idempotent via UNIQUE(symbol, signal_date)."""
    conn.execute(
        text("""
            INSERT INTO buy_watch_list
                (symbol, signal_date, ha_open, ha_high, ha_low, ha_close, trailing_stop)
            VALUES
                (:sym, :sd, :ho, :hh, :hl, :hc, :ts)
            ON CONFLICT (symbol, signal_date) DO NOTHING
        """),
        {
            "sym": r["symbol"],
            "sd":  r["signal_date"],
            "ho":  r["open"],
            "hh":  r["high"],
            "hl":  r["low"],
            "hc":  r["close"],
            "ts":  r["trailing_stop"],
        },
    )



def upsert_scan_result(
    conn,
    scan_date: date,
    symbol: str,
    signal: str,
    buy_date: Optional[date],
    buy_high: Optional[float],
    today_close: float,
) -> None:
    """Insert or update today's scan result."""

    conn.execute(
        text(
            """
            INSERT INTO scan_results
                (scan_date, symbol, signal, buy_signal_date, buy_high, today_close, breakout)
            VALUES
                (:sd, :sym, :sig, :bsd, :bh, :tc, :bo)
            ON CONFLICT (scan_date, symbol)
            DO UPDATE SET
                signal          = EXCLUDED.signal,
                buy_signal_date = EXCLUDED.buy_signal_date,
                buy_high        = EXCLUDED.buy_high,
                today_close     = EXCLUDED.today_close,
                breakout        = EXCLUDED.breakout
            """
        ),
        {
            "sd":  scan_date,
            "sym": symbol,
            "sig": signal,
            "bsd": buy_date,
            "bh":  buy_high or 0.0,
            "tc":  today_close,
            "bo":  (today_close > buy_high) if buy_high else False,
        },
    )


# ---------------------------------------------------------------------------
# Core scan logic
# ---------------------------------------------------------------------------


def scan_symbol(
    engine,
    symbol: str,
    scan_date: date,
    use_db: bool = True,
    days: int = 1,
) -> list[dict] | None:
    """
    Run the full pipeline for one symbol.

    Returns a list of dicts with signal info, or None if skipped.
    """

    # 1. Load OHLC
    df = load_ohlc_from_db(engine, symbol) if use_db else pd.DataFrame()

    if df.empty or len(df) < MIN_CANDLES:
        log.debug("%-15s  skipped (insufficient data: %d rows)", symbol, len(df))
        return None

    # 2. Heikin Ashi
    df = append_heikin_ashi(df)

    # 3. UT Bot
    df = compute_ut_bot(
        df,
        atr_period=UT_BOT_ATR_PERIOD,
        key_value=UT_BOT_KEY_VALUE,
        use_heikin_ashi=True,
    )

    # 4. Extract last N rows
    results = []
    actual_days = min(days, len(df))
    sub_df = df.tail(actual_days)

    for idx, row in sub_df.iterrows():
        signal: str = row["Signal"]
        # If we are doing a single day scan, record it anyway (even if Signal is NONE) to keep scan_results complete.
        # If we are doing a multi-day/historical report, we only care about actual BUY/SELL signals.
        if days == 1 or signal in (SIGNAL_BUY, SIGNAL_SELL):
            results.append({
                "symbol":        symbol,
                "scan_date":     idx.date(),
                "signal_date":   idx.date(),
                "signal":        signal,
                # Use HA OHLC — these are the values the UT Bot signals are computed from.
                # Raw candles are still stored in daily_candles for reference.
                "open":          float(row.get("HA_Open",  row["Open"])),
                "high":          float(row.get("HA_High",  row["High"])),
                "low":           float(row.get("HA_Low",   row["Low"])),
                "close":         float(row.get("HA_Close", row["Close"])),
                "trailing_stop": float(row["TrailingStop"]),
            })

    return results


# ---------------------------------------------------------------------------
# Main scan orchestrator
# ---------------------------------------------------------------------------


def run_scan(
    scan_date: date,
    use_db: bool = True,
    symbol: Optional[str] = None,
    days: int = 1,
) -> list[dict]:
    """
    Run the daily scan.

    Parameters
    ----------
    scan_date : date  — The date to stamp results with (usually today).
    use_db    : bool  — Persist results to PostgreSQL.
    symbol    : str   — Optional specific symbol to scan.
    days      : int   — Number of past days to scan for signals.
    """

    engine = get_engine()

    if symbol:
        symbols = [symbol.strip().upper().replace(".NS", "")]
    else:
        # Load symbols
        symbols = load_symbols_from_db(engine) if use_db else []
        if not symbols:
            csv_path = os.path.join(os.path.dirname(__file__), "symbols.csv")
            symbols = load_symbols_from_csv(csv_path)

    log.info("Scanning %d symbols for %s (lookback: %d days) …", len(symbols), scan_date, days)

    results: list[dict] = []
    buy_count  = 0
    sell_count = 0
    skip_count = 0
    scanned_count = 0

    for i, symbol in enumerate(symbols, start=1):

        try:
            symbol_results = scan_symbol(engine, symbol, scan_date, use_db=use_db, days=days)
        except Exception as exc:  # noqa: BLE001
            log.warning("%-15s  ERROR: %s", symbol, exc)
            skip_count += 1
            continue

        if symbol_results is None:
            skip_count += 1
            continue

        scanned_count += 1

        for r in symbol_results:
            results.append(r)
            sig = r["signal"]
            date_suffix = f" on {r['signal_date']}" if days > 1 else ""
            if sig == SIGNAL_BUY:
                buy_count += 1
                log.info("  %-15s  ✅ BUY%s   close=%.2f  stop=%.2f", symbol, date_suffix, r["close"], r["trailing_stop"])
            elif sig == SIGNAL_SELL:
                sell_count += 1
                log.info("  %-15s  🔴 SELL%s  close=%.2f  stop=%.2f", symbol, date_suffix, r["close"], r["trailing_stop"])

        if i % 50 == 0:
            log.info("Progress: %d / %d", i, len(symbols))

    # ------------------------------------------------------------------
    # Persist to DB
    # ------------------------------------------------------------------

    if use_db and results:
        log.info("Writing results to database …")
        with engine.begin() as conn:
            for r in results:
                sig = r["signal"]

                if sig == SIGNAL_BUY:
                    upsert_latest_buy(
                        conn,
                        r["symbol"],
                        r["signal_date"],
                        pd.Series({
                            "Open":  r["open"],
                            "High":  r["high"],
                            "Low":   r["low"],
                            "Close": r["close"],
                        }),
                    )
                    # Also add to the general buy_watch_list (idempotent)
                    upsert_buy_watch_list(conn, r)
                elif sig == SIGNAL_SELL:
                    delete_latest_buy(conn, r["symbol"])

                upsert_scan_result(
                    conn,
                    scan_date=r["scan_date"],
                    symbol=r["symbol"],
                    signal=sig,
                    buy_date=r["signal_date"] if sig == SIGNAL_BUY else None,
                    buy_high=r["high"]  if sig == SIGNAL_BUY else None,
                    today_close=r["close"],
                )

    actionable = buy_count + sell_count
    log.info(
        "\n─── Scan complete ───────────────────────────────\n"
        "  Date    : %s\n"
        "  Scanned : %d symbols  (%d skipped)\n"
        "  BUY     : %d\n"
        "  SELL    : %d\n"
        "  Signals : %d\n"
        "─────────────────────────────────────────────────",
        scan_date,
        scanned_count,
        skip_count,
        buy_count,
        sell_count,
        actionable,
    )

    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NSE UT Bot Daily Scanner")
    p.add_argument(
        "--date",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=date.today(),
        help="Scan date (default: today)",
    )
    p.add_argument(
        "--symbol",
        type=str,
        help="Scan only a single symbol (e.g., RELIANCE)",
    )
    p.add_argument(
        "--days",
        type=int,
        default=1,
        help="Number of past days/candles to scan for signals (default: 1)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_scan(
        scan_date=args.date,
        symbol=args.symbol,
        days=args.days,
    )

