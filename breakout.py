"""
breakout.py

UT Bot Breakout Confirmation Engine
====================================

All comparisons use Heikin Ashi (HA) values, consistent with how the UT Bot
signals themselves are generated.

Logic (runs after the daily scan):

  For each symbol with the most recent UT Bot signal = BUY (from scan_results):

    1. Get the HA OHLC of the BUY signal candle from scan_results
       (scan_results now stores HA values, not raw OHLC).

    2. Compute the HA candle's price change:
           change_pct = (HA_close - HA_open) / HA_open * 100

    3. SKIP the symbol if change_pct > MAX_BUY_CANDLE_CHANGE_PCT (default 10%).
       A candle that has already moved 10%+ is an exhaustion move — chasing it
       is high risk.

    4. Compute today's HA_Close by loading the full raw candle history for
       the symbol and recomputing Heikin Ashi (needed because HA is iterative).

    5. If today_ha_close > buy_ha_high:
           → Upsert the symbol into buy_watch_list as a confirmed breakout.
       Else:
           → Remove the symbol from buy_watch_list if it was previously there
             (breakout has since reversed / not yet triggered).

Usage:
    python breakout.py [--symbol SYMBOL] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(__file__))
from config import DATABASE_URL
from indicators.heikin_ashi import append_heikin_ashi

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
# Settings
# ---------------------------------------------------------------------------

MAX_BUY_CANDLE_CHANGE_PCT = 10.0  # skip if HA buy candle moved more than this %


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def get_engine():
    return create_engine(DATABASE_URL)


def load_latest_buy_signals(conn, symbol: str | None = None) -> list[dict]:
    """
    Return the most recent BUY signal per symbol from buy_watch_list.
    One row per symbol (the latest signal_date wins).
    """
    if symbol:
        rows = conn.execute(
            text("""
                SELECT DISTINCT ON (symbol)
                    symbol,
                    signal_date  AS buy_signal_date,
                    ha_open,
                    ha_high,
                    ha_low,
                    ha_close
                FROM buy_watch_list
                WHERE symbol = :sym
                ORDER BY symbol, signal_date DESC
            """),
            {"sym": symbol.strip().upper().replace(".NS", "")},
        ).fetchall()
    else:
        rows = conn.execute(
            text("""
                SELECT DISTINCT ON (symbol)
                    symbol,
                    signal_date  AS buy_signal_date,
                    ha_open,
                    ha_high,
                    ha_low,
                    ha_close
                FROM buy_watch_list
                ORDER BY symbol, signal_date DESC
            """)
        ).fetchall()

    return [dict(r._mapping) for r in rows]


def load_ha_close_today(conn, symbol: str) -> tuple[date | None, float | None]:
    """
    Recompute Heikin Ashi from full raw candle history and return today's HA_Close.

    HA is an iterative calculation — we need the full history to get an
    accurate today value that matches the scanner's own computation.
    """
    rows = conn.execute(
        text("""
            SELECT candle_date, open, high, low, close
            FROM daily_candles
            WHERE symbol = :sym
            ORDER BY candle_date
        """),
        {"sym": symbol},
    ).fetchall()

    if not rows:
        return None, None

    df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close"])
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date")

    ha_df = append_heikin_ashi(df)
    last_row = ha_df.iloc[-1]
    return last_row.name.date(), float(last_row["HA_Close"])


def upsert_confirmed_breakout(conn, record: dict) -> None:
    """Upsert a confirmed breakout into the confirmed_breakouts table."""
    conn.execute(
        text("""
            INSERT INTO confirmed_breakouts (
                symbol,
                signal_date,
                buy_candle_open,
                buy_candle_high,
                buy_candle_low,
                buy_candle_close,
                buy_candle_range_pct,
                confirmation_date,
                confirmed_close
            ) VALUES (
                :symbol,
                :signal_date,
                :buy_candle_open,
                :buy_candle_high,
                :buy_candle_low,
                :buy_candle_close,
                :buy_candle_range_pct,
                :confirmation_date,
                :confirmed_close
            )
            ON CONFLICT (symbol) DO UPDATE SET
                signal_date          = EXCLUDED.signal_date,
                buy_candle_open      = EXCLUDED.buy_candle_open,
                buy_candle_high      = EXCLUDED.buy_candle_high,
                buy_candle_low       = EXCLUDED.buy_candle_low,
                buy_candle_close     = EXCLUDED.buy_candle_close,
                buy_candle_range_pct = EXCLUDED.buy_candle_range_pct,
                confirmation_date    = EXCLUDED.confirmation_date,
                confirmed_close      = EXCLUDED.confirmed_close,
                updated_at           = NOW()
        """),
        record,
    )


def remove_from_confirmed_breakouts(conn, symbol: str) -> None:
    """Remove symbol from confirmed_breakouts if breakout has reversed."""
    conn.execute(
        text("DELETE FROM confirmed_breakouts WHERE symbol = :sym"),
        {"sym": symbol},
    )


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def run_breakout(
    symbol: str | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """
    Run the breakout confirmation pass using Heikin Ashi values throughout.

    Returns a list of confirmed breakout dicts.
    """
    engine = get_engine()
    confirmed: list[dict] = []
    skipped_gap = 0
    skipped_no_data = 0
    not_broken_out = 0

    with engine.begin() as conn:
        buy_signals = load_latest_buy_signals(conn, symbol)

        if not buy_signals:
            log.info("No BUY signals found in scan_results.")
            return []

        log.info("Evaluating %d BUY signals for breakout confirmation (HA values) …", len(buy_signals))

        for sig in buy_signals:
            sym          = sig["symbol"]
            buy_sig_date = sig["buy_signal_date"]

            if buy_sig_date is None:
                skipped_no_data += 1
                continue

            # ----------------------------------------------------------------
            # 1. BUY candle HA OHLC — read from scan_results (HA values stored
            #    since scanner fix; fallback to buy_high if OHLC cols are NULL)
            # ----------------------------------------------------------------
            ha_open  = sig.get("ha_open")  or sig.get("buy_high")
            ha_high  = sig.get("ha_high")  or sig.get("buy_high")
            ha_low   = sig.get("ha_low")   or sig.get("ha_high")
            ha_close = sig.get("ha_close") or sig.get("buy_high")

            if ha_high is None:
                log.debug("%-15s  no HA data in scan_results, skipping", sym)
                skipped_no_data += 1
                continue

            ha_open  = float(ha_open)
            ha_high  = float(ha_high)
            ha_low   = float(ha_low)
            ha_close = float(ha_close)

            # ----------------------------------------------------------------
            # 2. Candle change % filter (skip exhaustion gap candles)
            #    Measured as high-to-low range to capture the full candle move,
            #    not just the open-to-close body.
            # ----------------------------------------------------------------
            change_pct = (ha_high - ha_low) / ha_low * 100 if ha_low else 0.0

            if abs(change_pct) > MAX_BUY_CANDLE_CHANGE_PCT:
                log.info(
                    "  %-15s  ⚠️  SKIP — HA buy candle moved %.1f%% (threshold %.1f%%)",
                    sym, change_pct, MAX_BUY_CANDLE_CHANGE_PCT,
                )
                skipped_gap += 1
                if not dry_run:
                    remove_from_confirmed_breakouts(conn, sym)
                continue

            # ----------------------------------------------------------------
            # 3. Today's HA_Close (recomputed from full raw history)
            # ----------------------------------------------------------------
            today_date, today_ha_close = load_ha_close_today(conn, sym)

            if today_ha_close is None:
                skipped_no_data += 1
                continue

            # ----------------------------------------------------------------
            # 4. Breakout check: today HA_Close > buy candle HA_High
            # ----------------------------------------------------------------
            if today_ha_close > ha_high:
                pct_above = (today_ha_close - ha_high) / ha_high * 100
                log.info(
                    "  %-15s  ✅ BREAKOUT  ha_buy_high=%.2f  ha_today_close=%.2f  (+%.1f%%)",
                    sym, ha_high, today_ha_close, pct_above,
                )

                record = {
                    "symbol":               sym,
                    "signal_date":          buy_sig_date,
                    "buy_candle_open":      ha_open,
                    "buy_candle_high":      ha_high,
                    "buy_candle_low":       ha_low,
                    "buy_candle_close":     ha_close,
                    "buy_candle_range_pct": round(change_pct, 4),
                    "confirmation_date":    today_date,
                    "confirmed_close":      today_ha_close,
                }

                confirmed.append(record)

                if not dry_run:
                    upsert_confirmed_breakout(conn, record)

            else:
                not_broken_out += 1
                log.debug(
                    "  %-15s  — no breakout  ha_buy_high=%.2f  ha_today_close=%.2f",
                    sym, ha_high, today_ha_close,
                )
                # Remove from confirmed_breakouts if it previously was confirmed but pulled back
                if not dry_run:
                    remove_from_confirmed_breakouts(conn, sym)

    log.info(
        "\n─── Breakout scan complete ──────────────────────────\n"
        "  Evaluated    : %d symbols\n"
        "  ✅ Confirmed  : %d  (added to confirmed_breakouts)\n"
        "  — Not yet    : %d\n"
        "  ⚠️  Gap-skip  : %d  (HA candle range >%.0f%%)\n"
        "  ⚠️  No data   : %d\n"
        "────────────────────────────────────────────────────",
        len(buy_signals),
        len(confirmed),
        not_broken_out,
        skipped_gap,
        MAX_BUY_CANDLE_CHANGE_PCT,
        skipped_no_data,
    )

    return confirmed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="UT Bot Breakout Confirmation Engine (Heikin Ashi)")
    p.add_argument(
        "--symbol",
        type=str,
        help="Only evaluate a single symbol (e.g., RELIANCE)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results but do not write to buy_watch_list",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_breakout(symbol=args.symbol, dry_run=args.dry_run)
