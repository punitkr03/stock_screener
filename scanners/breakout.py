"""
breakout.py

UT Bot Breakout Confirmation Engine
====================================

All comparisons use Heikin Ashi (HA) values, consistent with how the UT Bot
signals themselves are generated.

Logic (runs after the daily scan):

  For each symbol in buy_watch_list (latest BUY signal per symbol):

    1. Get the HA OHLC of the BUY signal candle from buy_watch_list
       (HA values are stored there by the scanner at signal time).

    2. Compute the candle's high-to-low range:
           change_pct = (HA_high - HA_low) / HA_low * 100

    3. SKIP the symbol if change_pct > MAX_BUY_CANDLE_CHANGE_PCT (default 10%).
       A candle that swung more than 10% high-to-low is an exhaustion move —
       chasing it is high risk.

    4. Recompute the full HA candle history + UT Bot signals from daily_candles.

    5. Two-stage breakout check (both using HA values):

       a) NEXT-DAY (T+1) — strict immediate follow-through:
              candle immediately after BUY signal candle HA_Close > buy HA_High
              → Confirmed.

       b) CURRENT-DAY — deferred breakout, today's close above buy level:
              Today's HA_Close > buy HA_High
              AND no SELL signal between the buy signal date and today (inclusive).
              → Confirmed.

       If EITHER condition is met the symbol is upserted into confirmed_breakouts.
       If NEITHER condition is met the symbol is removed from confirmed_breakouts.

Usage:
    python breakout.py [--symbol SYMBOL] [--dry-run]
"""

import argparse
import logging
import os
import sys
from datetime import date

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd
from sqlalchemy import create_engine, text

from config import DATABASE_URL, UT_BOT_ATR_PERIOD, UT_BOT_KEY_VALUE, load_index_symbols
from indicators.heikin_ashi import append_heikin_ashi
from indicators.ut_bot import SIGNAL_SELL, compute_ut_bot

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
    Return the most recent BUY signal per symbol from buy_watch_list (excluding indices).
    One row per symbol (the latest signal_date wins).
    """
    index_symbols = load_index_symbols()

    if symbol:
        clean_sym = symbol.strip().upper().replace(".NS", "")
        if clean_sym in index_symbols:
            return []
        rows = conn.execute(
            text("""
                SELECT DISTINCT ON (b.symbol)
                    b.symbol,
                    b.signal_date  AS buy_signal_date,
                    b.ha_open,
                    b.ha_high,
                    b.ha_low,
                    b.ha_close
                FROM buy_watch_list b
                LEFT JOIN stocks s ON s.symbol = b.symbol
                WHERE b.symbol = :sym AND (s.exchange IS NULL OR s.exchange = 'NSE')
                ORDER BY b.symbol, b.signal_date DESC
            """),
            {"sym": clean_sym},
        ).fetchall()
    else:
        rows = conn.execute(
            text("""
                SELECT DISTINCT ON (b.symbol)
                    b.symbol,
                    b.signal_date  AS buy_signal_date,
                    b.ha_open,
                    b.ha_high,
                    b.ha_low,
                    b.ha_close
                FROM buy_watch_list b
                LEFT JOIN stocks s ON s.symbol = b.symbol
                WHERE (s.exchange IS NULL OR s.exchange = 'NSE')
                ORDER BY b.symbol, b.signal_date DESC
            """)
        ).fetchall()

    return [dict(r._mapping) for r in rows if r._mapping["symbol"].strip().upper() not in index_symbols]


def load_ha_dataframe(conn, symbol: str) -> pd.DataFrame:
    """
    Load the full raw candle history and return a DataFrame with HA + UT Bot
    Signal columns appended.

    HA is iterative — full history is needed for an accurate value.
    The UT Bot is also path-dependent (Wilder ATR), so we compute both on the
    complete history.
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
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close"])
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date")

    df = append_heikin_ashi(df)
    df = compute_ut_bot(
        df,
        atr_period=UT_BOT_ATR_PERIOD,
        key_value=UT_BOT_KEY_VALUE,
        use_heikin_ashi=True,
    )
    return df


# ---------------------------------------------------------------------------
# Breakout checkers
# ---------------------------------------------------------------------------


def _resolve_signal_ts(ha_df: pd.DataFrame, signal_date) -> pd.Timestamp | None:
    """
    Find the index timestamp for signal_date in ha_df.
    If the exact date is missing (holiday, gap), fall back to the nearest
    prior trading day.
    """
    signal_ts = pd.Timestamp(signal_date)
    if signal_ts in ha_df.index:
        return signal_ts
    prior = ha_df.index[ha_df.index <= signal_ts]
    return prior[-1] if not prior.empty else None


def check_next_day_breakout(
    ha_df: pd.DataFrame,
    signal_date,
    buy_ha_high: float,
) -> tuple[date | None, float | None]:
    """
    Check whether the candle IMMEDIATELY after signal_date closes above
    buy_ha_high (T+1 strict follow-through), AND that no SELL signal has
    appeared anywhere between the buy signal and today (inclusive).

    Even though this check anchors on T+1's price, a SELL anywhere in the
    post-buy window means the trade thesis is invalidated — the historical
    T+1 confirmation must not override a later reversal.

    Returns (confirmation_date, confirmed_ha_close) or (None, None).
    """
    signal_ts = _resolve_signal_ts(ha_df, signal_date)
    if signal_ts is None:
        return None, None

    pos = ha_df.index.get_loc(signal_ts)
    if pos + 1 >= len(ha_df):
        # No next candle yet (signal fired on the most recent bar)
        return None, None

    next_row = ha_df.iloc[pos + 1]
    next_ha_close = float(next_row["HA_Close"])

    # Discard if ANY candle after the buy signal carries a SELL — thesis reversed.
    # This mirrors check_current_day_breakout and ensures a later SELL always
    # wins, even when T+1 would otherwise qualify as a valid breakout.
    window = ha_df.iloc[pos + 1:]
    if SIGNAL_SELL in window["Signal"].values:
        return None, None

    if next_ha_close > buy_ha_high:
        return next_row.name.date(), next_ha_close
    return None, None


def check_current_day_breakout(
    ha_df: pd.DataFrame,
    signal_date,
    buy_ha_high: float,
) -> tuple[date | None, float | None]:
    """
    Check whether the MOST RECENT candle closes above buy_ha_high,
    provided no SELL signal occurred between signal_date (exclusive) and
    the current bar (inclusive).

    The "no-sell" constraint ensures we only count the deferred breakout
    while the original BUY thesis is still intact.

    Returns (confirmation_date, confirmed_ha_close) or (None, None).
    """
    signal_ts = _resolve_signal_ts(ha_df, signal_date)
    if signal_ts is None:
        return None, None

    pos = ha_df.index.get_loc(signal_ts)
    # Slice from the candle AFTER the buy signal up to (and including) today
    window = ha_df.iloc[pos + 1:]

    if window.empty:
        return None, None

    # Check for any SELL signal in this window — if found, breakout is void
    if SIGNAL_SELL in window["Signal"].values:
        return None, None

    # Compare today's (last bar's) HA_Close against the buy signal HA_High
    today_row = window.iloc[-1]
    today_ha_close = float(today_row["HA_Close"])

    if today_ha_close > buy_ha_high:
        return today_row.name.date(), today_ha_close
    return None, None


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
            # 1. BUY candle HA OHLC — read from buy_watch_list
            #    (HA values stored by the scanner at signal time)
            # ----------------------------------------------------------------
            ha_open  = sig.get("ha_open")
            ha_high  = sig.get("ha_high")
            ha_low   = sig.get("ha_low")
            ha_close = sig.get("ha_close")

            if ha_high is None:
                log.debug("%-15s  no HA data in buy_watch_list, skipping", sym)
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
            # 3. Load full HA + UT Bot data (recomputed from scratch)
            # ----------------------------------------------------------------
            ha_df = load_ha_dataframe(conn, sym)

            if ha_df.empty:
                log.debug("  %-15s  — no candle data in DB, skipping", sym)
                skipped_no_data += 1
                continue

            # ----------------------------------------------------------------
            # 4a. T+1 breakout check (immediate next candle)
            # ----------------------------------------------------------------
            next_date, next_ha_close = check_next_day_breakout(ha_df, buy_sig_date, ha_high)

            # ----------------------------------------------------------------
            # 4b. Current-day breakout check (deferred, no SELL in between)
            # ----------------------------------------------------------------
            cur_date, cur_ha_close = check_current_day_breakout(ha_df, buy_sig_date, ha_high)

            # ----------------------------------------------------------------
            # 5. Decide: prefer next_day if both fire; fall back to current_day
            # ----------------------------------------------------------------
            if next_ha_close is not None:
                pct_above = (next_ha_close - ha_high) / ha_high * 100
                conf_date, conf_close = next_date, next_ha_close
                log.info(
                    "  %-15s  ✅ BREAKOUT (next_day)     ha_buy_high=%.2f  next_ha_close=%.2f  (+%.1f%%)  [%s]",
                    sym, ha_high, conf_close, pct_above, conf_date,
                )
            elif cur_ha_close is not None:
                pct_above = (cur_ha_close - ha_high) / ha_high * 100
                conf_date, conf_close = cur_date, cur_ha_close
                log.info(
                    "  %-15s  ✅ BREAKOUT (current_day)  ha_buy_high=%.2f  confirmed_close=%.2f  (+%.1f%%)  [%s]",
                    sym, ha_high, conf_close, pct_above, conf_date,
                )
            else:
                not_broken_out += 1
                log.debug(
                    "  %-15s  — no breakout  ha_buy_high=%.2f  [buy_date: %s]",
                    sym, ha_high, buy_sig_date,
                )
                # Remove from confirmed_breakouts if previously confirmed but now reversed
                if not dry_run:
                    remove_from_confirmed_breakouts(conn, sym)
                continue

            record = {
                "symbol":               sym,
                "signal_date":          buy_sig_date,
                "buy_candle_open":      ha_open,
                "buy_candle_high":      ha_high,
                "buy_candle_low":       ha_low,
                "buy_candle_close":     ha_close,
                "buy_candle_range_pct": round(change_pct, 4),
                "confirmation_date":    conf_date,
                "confirmed_close":      conf_close,
            }

            confirmed.append(record)

            if not dry_run:
                upsert_confirmed_breakout(conn, record)

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
