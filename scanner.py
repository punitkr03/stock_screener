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
import csv
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

SIGNALS_DIR = os.path.join(os.path.dirname(__file__), "signals")
MIN_CANDLES = 30  # minimum rows needed to compute meaningful ATR

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
) -> dict | None:
    """
    Run the full pipeline for one symbol.

    Returns a dict with signal info, or None if skipped.
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

    # 4. Extract last row
    last = df.iloc[-1]
    signal: str = last["Signal"]
    last_date: date = df.index[-1].date()

    return {
        "symbol":       symbol,
        "scan_date":    scan_date,
        "signal_date":  last_date,
        "signal":       signal,
        "open":         float(last["Open"]),
        "high":         float(last["High"]),
        "low":          float(last["Low"]),
        "close":        float(last["Close"]),
        "trailing_stop": float(last["TrailingStop"]),
    }


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------


def write_signals_csv(results: list[dict], scan_date: date) -> str:
    """Write BUY/SELL signals to a dated CSV file. Returns the file path."""

    os.makedirs(SIGNALS_DIR, exist_ok=True)
    path = os.path.join(SIGNALS_DIR, f"signals_{scan_date}.csv")

    fieldnames = [
        "symbol",
        "signal",
        "signal_date",
        "open",
        "high",
        "low",
        "close",
        "trailing_stop",
    ]

    actionable = [r for r in results if r["signal"] in (SIGNAL_BUY, SIGNAL_SELL)]

    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(actionable)

    return path


# ---------------------------------------------------------------------------
# Main scan orchestrator
# ---------------------------------------------------------------------------


def run_scan(
    scan_date: date,
    use_db: bool = True,
    csv_only: bool = False,
) -> list[dict]:
    """
    Run the full daily scan.

    Parameters
    ----------
    scan_date : date  — The date to stamp results with (usually today).
    use_db    : bool  — Persist results to PostgreSQL.
    csv_only  : bool  — Skip DB writes; only write CSV.
    """

    engine = get_engine()

    # Load symbols
    symbols = load_symbols_from_db(engine) if use_db else []
    if not symbols:
        csv_path = os.path.join(os.path.dirname(__file__), "symbols.csv")
        symbols = load_symbols_from_csv(csv_path)

    log.info("Scanning %d symbols for %s …", len(symbols), scan_date)

    results: list[dict] = []
    buy_count  = 0
    sell_count = 0
    skip_count = 0

    for i, symbol in enumerate(symbols, start=1):

        try:
            result = scan_symbol(engine, symbol, scan_date, use_db=use_db)
        except Exception as exc:  # noqa: BLE001
            log.warning("%-15s  ERROR: %s", symbol, exc)
            skip_count += 1
            continue

        if result is None:
            skip_count += 1
            continue

        results.append(result)

        sig = result["signal"]
        if sig == SIGNAL_BUY:
            buy_count += 1
            log.info("  %-15s  ✅ BUY   close=%.2f  stop=%.2f", symbol, result["close"], result["trailing_stop"])
        elif sig == SIGNAL_SELL:
            sell_count += 1
            log.info("  %-15s  🔴 SELL  close=%.2f  stop=%.2f", symbol, result["close"], result["trailing_stop"])

        if i % 50 == 0:
            log.info("Progress: %d / %d", i, len(symbols))

    # ------------------------------------------------------------------
    # Persist to DB
    # ------------------------------------------------------------------

    if use_db and not csv_only and results:
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

                upsert_scan_result(
                    conn,
                    scan_date=r["scan_date"],
                    symbol=r["symbol"],
                    signal=sig,
                    buy_date=r["signal_date"] if sig == SIGNAL_BUY else None,
                    buy_high=r["high"]  if sig == SIGNAL_BUY else None,
                    today_close=r["close"],
                )

    # ------------------------------------------------------------------
    # Write CSV
    # ------------------------------------------------------------------

    csv_path = write_signals_csv(results, scan_date)

    actionable = buy_count + sell_count
    log.info(
        "\n─── Scan complete ───────────────────────────────\n"
        "  Date    : %s\n"
        "  Scanned : %d symbols  (%d skipped)\n"
        "  BUY     : %d\n"
        "  SELL    : %d\n"
        "  Signals : %d\n"
        "  CSV     : %s\n"
        "─────────────────────────────────────────────────",
        scan_date,
        len(results),
        skip_count,
        buy_count,
        sell_count,
        actionable,
        csv_path,
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
        "--csv-only",
        action="store_true",
        help="Write CSV only; skip DB writes",
    )
    p.add_argument(
        "--no-db",
        action="store_true",
        help="Do not read OHLC from DB (uses yfinance fallback — not yet implemented)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_scan(
        scan_date=args.date,
        use_db=not args.no_db,
        csv_only=args.csv_only,
    )
