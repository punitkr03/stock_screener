"""
processor.py
------------
Orchestrates fundamental metrics calculation for buy-confirmed stocks
(confirmed_breakouts table).

Features:
- Incremental: only processes symbols where metrics_data IS NULL by default.
- CLI support: --symbol, --force, --dry-run.
- Safely updates confirmed_breakouts table with JSON metrics_data payload.

Usage:
    python analytics/processor.py [--symbol RELIANCE] [--force] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

from config import DATABASE_URL, load_index_symbols
from analytics.engine import calculate_stock_metrics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def get_engine():
    return create_engine(DATABASE_URL)


def check_column_exists(conn, table_name: str, column_name: str) -> bool:
    """Check whether a column exists on a PostgreSQL table."""
    result = conn.execute(
        text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = :tbl AND column_name = :col
        """),
        {"tbl": table_name, "col": column_name},
    ).fetchone()
    return result is not None


def ensure_metrics_column(conn) -> None:
    """Ensure the metrics_data JSONB column exists on confirmed_breakouts."""
    if not check_column_exists(conn, "confirmed_breakouts", "metrics_data"):
        try:
            conn.execute(
                text("ALTER TABLE confirmed_breakouts ADD COLUMN IF NOT EXISTS metrics_data JSONB")
            )
            log.info("Added 'metrics_data' column to 'confirmed_breakouts'.")
        except Exception as exc:
            log.warning("Could not auto-add 'metrics_data' column: %s", exc)


def load_pending_symbols(
    conn,
    symbol: str | None = None,
    force: bool = False,
) -> list[str]:
    """
    Fetch symbols from confirmed_breakouts that need metrics calculated (excluding indices).
    If force=False, returns only rows where metrics_data IS NULL.
    """
    ensure_metrics_column(conn)
    index_symbols = load_index_symbols()

    if symbol:
        clean_sym = symbol.strip().upper().replace(".NS", "")
        if clean_sym in index_symbols:
            return []
        if force:
            query = """
                SELECT c.symbol FROM confirmed_breakouts c
                LEFT JOIN stocks s ON s.symbol = c.symbol
                WHERE c.symbol = :sym AND (s.exchange IS NULL OR s.exchange = 'NSE')
            """
        else:
            query = """
                SELECT c.symbol FROM confirmed_breakouts c
                LEFT JOIN stocks s ON s.symbol = c.symbol
                WHERE c.symbol = :sym AND c.metrics_data IS NULL AND (s.exchange IS NULL OR s.exchange = 'NSE')
            """
        rows = conn.execute(text(query), {"sym": clean_sym}).fetchall()
    else:
        if force:
            query = """
                SELECT c.symbol FROM confirmed_breakouts c
                LEFT JOIN stocks s ON s.symbol = c.symbol
                WHERE (s.exchange IS NULL OR s.exchange = 'NSE')
                ORDER BY c.confirmation_date DESC, c.symbol
            """
        else:
            query = """
                SELECT c.symbol FROM confirmed_breakouts c
                LEFT JOIN stocks s ON s.symbol = c.symbol
                WHERE c.metrics_data IS NULL AND (s.exchange IS NULL OR s.exchange = 'NSE')
                ORDER BY c.confirmation_date DESC, c.symbol
            """
        rows = conn.execute(text(query)).fetchall()

    return [r[0] for r in rows if r[0].strip().upper() not in index_symbols]


def update_metrics_in_db(conn, symbol: str, metrics: dict) -> None:
    """Store the metrics JSON payload into confirmed_breakouts."""
    conn.execute(
        text("""
            UPDATE confirmed_breakouts
            SET metrics_data = :data,
                updated_at   = NOW()
            WHERE symbol = :sym
        """),
        {
            "sym": symbol,
            "data": json.dumps(metrics),
        },
    )


def process_confirmed_metrics(
    symbol: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> list[dict]:
    """
    Compute fundamental valuation & solvency metrics for confirmed breakout stocks.
    
    Returns
    -------
    list[dict]
        List of processed metric payloads.
    """
    engine = get_engine()
    processed: list[dict] = []

    with engine.begin() as conn:
        symbols = load_pending_symbols(conn, symbol=symbol, force=force)

        if not symbols:
            if symbol and dry_run:
                clean_sym = symbol.strip().upper().replace(".NS", "")
                log.info("Symbol %s not found in confirmed_breakouts table, evaluating in dry-run mode anyway …", clean_sym)
                symbols = [clean_sym]
            else:
                log.info("No pending confirmed breakout symbols for metrics calculation (all up-to-date).")
                return []

        log.info("Processing fundamental metrics for %d confirmed breakout symbol(s) …", len(symbols))

        for idx, sym in enumerate(symbols, start=1):
            log.info("[%d/%d] Calculating metrics for %s …", idx, len(symbols), sym)
            try:
                metrics = calculate_stock_metrics(sym)
                processed.append(metrics)

                val = metrics.get("valuation", {})
                sol = metrics.get("solvency", {})

                log.info(
                    "  %-12s | PE: %s (Sec: %s) | PB: %s (Sec: %s) | EPS: %s | Fair Price: %s | Int. Cov: %sx",
                    sym,
                    val.get("pe"),
                    val.get("sector_pe"),
                    val.get("pb"),
                    val.get("sector_pb"),
                    val.get("eps"),
                    val.get("fair_price"),
                    sol.get("interest_coverage_ratio"),
                )

                if dry_run:
                    log.info("  [dry-run] JSON payload:\n%s", json.dumps(metrics, indent=2))
                else:
                    update_metrics_in_db(conn, sym, metrics)

            except Exception as exc:
                log.error("Failed to compute metrics for %s: %s", sym, exc)

            time.sleep(0.1)

    log.info("Completed metrics processing for %d symbol(s).", len(processed))
    return processed


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stock Fundamentals & Valuation Analytics Engine")
    p.add_argument(
        "--symbol",
        type=str,
        help="Compute metrics for a single specified symbol (e.g. RELIANCE)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Force recalculation even if metrics_data already exists",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and display metrics without writing to database",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    process_confirmed_metrics(
        symbol=args.symbol,
        force=args.force,
        dry_run=args.dry_run,
    )
