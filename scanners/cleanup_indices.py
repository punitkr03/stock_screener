"""
cleanup_indices.py
------------------
One-time / maintenance utility to purge index symbols from stock signal tables
(buy_watch_list, confirmed_breakouts, latest_buy_signal) and ensure their
exchange is set to 'NSE_INDEX' in the stocks master table.
"""

import logging
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sqlalchemy import create_engine, text
from config import DATABASE_URL, load_index_symbols

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def cleanup():
    engine = create_engine(DATABASE_URL)
    index_symbols = list(load_index_symbols())

    if not index_symbols:
        log.warning("No index symbols found in nifty_indices.json")
        return

    log.info("Loaded %d index symbols to purge from stock watchlists.", len(index_symbols))

    with engine.begin() as conn:
        # 1. Update stocks table exchange='NSE_INDEX'
        res1 = conn.execute(
            text("""
                UPDATE stocks
                SET exchange = 'NSE_INDEX', updated_at = NOW()
                WHERE symbol = ANY(:syms) OR symbol LIKE 'NIFTY%'
            """),
            {"syms": index_symbols},
        )
        log.info("Updated %d index records in 'stocks' table to exchange='NSE_INDEX'.", res1.rowcount)

        # 2. Delete from confirmed_breakouts
        res2 = conn.execute(
            text("""
                DELETE FROM confirmed_breakouts
                WHERE symbol = ANY(:syms) OR symbol LIKE 'NIFTY%'
            """),
            {"syms": index_symbols},
        )
        log.info("Deleted %d index rows from 'confirmed_breakouts'.", res2.rowcount)

        # 3. Delete from buy_watch_list
        res3 = conn.execute(
            text("""
                DELETE FROM buy_watch_list
                WHERE symbol = ANY(:syms) OR symbol LIKE 'NIFTY%'
            """),
            {"syms": index_symbols},
        )
        log.info("Deleted %d index rows from 'buy_watch_list'.", res3.rowcount)

        # 4. Delete from latest_buy_signal
        res4 = conn.execute(
            text("""
                DELETE FROM latest_buy_signal
                WHERE symbol = ANY(:syms) OR symbol LIKE 'NIFTY%'
            """),
            {"syms": index_symbols},
        )
        log.info("Deleted %d index rows from 'latest_buy_signal'.", res4.rowcount)

    log.info("Cleanup complete.")


if __name__ == "__main__":
    cleanup()
