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


def load_ohlc_from_db(engine, symbol: str) -> pd.DataFrame:
    """
    Fetch the complete daily candle history for a symbol.

    We always load the full history (no LIMIT) because Wilder's ATR and the
    iterative HA/trailing-stop calculations are path-dependent — truncating
    history shifts the ATR seed and can flip signals compared to TradingView.

    Returns a DataFrame with columns: Date (index), Open, High, Low, Close, Volume.
    """

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT candle_date, open, high, low, close, volume
                FROM   daily_candles
                WHERE  symbol = :sym
                ORDER  BY candle_date ASC
                """
            ),
            {"sym": symbol},
        ).fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date")
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
    Each dict also carries an ``active_buy`` key (the last unresolved BUY
    signal from full history) so the caller can populate buy_watch_list
    with the correct historical signal_date.
    """

    # 1. Load full OHLC history (no limit — ATR is path-dependent)
    df = load_ohlc_from_db(engine, symbol) if use_db else pd.DataFrame()

    if df.empty or len(df) < MIN_CANDLES:
        log.debug("%-15s  skipped (insufficient data: %d rows)", symbol, len(df))
        return None

    # 2. Heikin Ashi
    df = append_heikin_ashi(df)

    # 3. UT Bot (on full history — needed for correct ATR seed)
    df = compute_ut_bot(
        df,
        atr_period=UT_BOT_ATR_PERIOD,
        key_value=UT_BOT_KEY_VALUE,
        use_heikin_ashi=True,
    )

    # 4. Find the last active BUY from full history.
    #    Walk backwards: if the most recent signal is BUY → it's active.
    #    If it's SELL → no active BUY. If NONE → keep scanning back.
    active_buy_row = None
    for idx_h in reversed(df.index):
        sig_h = df.at[idx_h, "Signal"]
        if sig_h == SIGNAL_BUY:
            active_buy_row = (idx_h, df.loc[idx_h])
            break
        elif sig_h == SIGNAL_SELL:
            break  # cancelled by a sell

    # 5. Extract last N rows for reporting BUY/SELL signals on those days
    results = []
    actual_days = min(days, len(df))
    sub_df = df.tail(actual_days)

    for idx, row in sub_df.iterrows():
        signal: str = row["Signal"]
        # Only store actionable signals — BUY and SELL.
        # NONE signals are not persisted to keep the table clean.
        if signal in (SIGNAL_BUY, SIGNAL_SELL):
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
                "active_buy":    None,
            })

    # Attach the active_buy to every result row so the DB block can use it.
    # If no result row exists (today is NONE), we still need to convey it —
    # add a synthetic NONE row so the active_buy upsert path is reached.
    if active_buy_row is not None:
        ab_idx, ab_row = active_buy_row
        active_buy_dict = {
            "symbol":        symbol,
            "signal_date":   ab_idx.date(),
            "open":          float(ab_row.get("HA_Open",  ab_row["Open"])),
            "high":          float(ab_row.get("HA_High",  ab_row["High"])),
            "low":           float(ab_row.get("HA_Low",   ab_row["Low"])),
            "close":         float(ab_row.get("HA_Close", ab_row["Close"])),
            "trailing_stop": float(ab_row["TrailingStop"]),
        }
        for r in results:
            r["active_buy"] = active_buy_dict

        # If no BUY/SELL fired today, still surface the active_buy so the
        # DB write block can upsert buy_watch_list with the correct date.
        if not results:
            results.append({
                "symbol":     symbol,
                "scan_date":  df.index[-1].date(),
                "signal_date": df.index[-1].date(),
                "signal":     SIGNAL_NONE,
                "open":       float(df["HA_Open"].iloc[-1]),
                "high":       float(df["HA_High"].iloc[-1]),
                "low":        float(df["HA_Low"].iloc[-1]),
                "close":      float(df["HA_Close"].iloc[-1]),
                "trailing_stop": float(df["TrailingStop"].iloc[-1]),
                "active_buy": active_buy_dict,
            })
    else:
        # No active BUY → mark for deletion from buy_watch_list
        for r in results:
            r["active_buy"] = None
        if not results:
            results.append({
                "symbol":     symbol,
                "scan_date":  df.index[-1].date(),
                "signal_date": df.index[-1].date(),
                "signal":     SIGNAL_NONE,
                "open":       float(df["HA_Open"].iloc[-1]),
                "high":       float(df["HA_High"].iloc[-1]),
                "low":        float(df["HA_Low"].iloc[-1]),
                "close":      float(df["HA_Close"].iloc[-1]),
                "trailing_stop": float(df["TrailingStop"].iloc[-1]),
                "active_buy": None,
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
                active_buy = r.get("active_buy")

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

                elif sig == SIGNAL_SELL:
                    delete_latest_buy(conn, r["symbol"])

                # Sync buy_watch_list using the historically correct active BUY.
                # active_buy is the last BUY from full history that hasn't been
                # cancelled by a subsequent SELL — regardless of whether today
                # fired a new BUY signal.
                if active_buy is not None:
                    upsert_buy_watch_list(conn, active_buy)
                elif sig == SIGNAL_SELL:
                    # SELL cancels any existing watch list entry
                    conn.execute(
                        text("DELETE FROM buy_watch_list WHERE symbol = :sym"),
                        {"sym": r["symbol"]},
                    )
                    conn.execute(
                        text("DELETE FROM confirmed_breakouts WHERE symbol = :sym"),
                        {"sym": r["symbol"]},
                    )

                # Only persist actual BUY/SELL signals in scan_results (not NONE)
                if sig in (SIGNAL_BUY, SIGNAL_SELL):
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

