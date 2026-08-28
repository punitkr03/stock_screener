#!/usr/bin/env python
"""
open_charts.py

Queries the database for symbols in the watch lists (buy_watch_list or confirmed_breakouts),
writes them to JSON files, then opens them in TradingView charts in the default browser.

Outputs TWO JSON files on every run:
    buy_confirmed_watchlist.json  - symbols with a confirmed breakout (confirmed_breakouts table)
    buy_signal_watchlist.json     - all symbols with an active BUY signal (buy_watch_list table)

JSON output structure per entry:
    id          - sequential integer
    symbol      - NSE symbol
    punit_link  - Punit's TradingView layout link
    vivek_link  - Vivek's TradingView layout link
    signal_date - date the BUY signal fired
"""

import argparse
import json
import os
import sys
import urllib.parse
import webbrowser
import time
from datetime import date

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sqlalchemy import create_engine, text
from config import (
    DATABASE_URL,
    MONGO_COLLECTION_BUY_CONFIRMED,
    MONGO_COLLECTION_BUY_SIGNAL,
    BUY_CONFIRMED_JSON,
    BUY_SIGNAL_JSON,
)
from db.mongo import write_collection

# ---------------------------------------------------------------------------
# TradingView chart layout IDs
# ---------------------------------------------------------------------------

PUNIT_CHART_ID = "zMHJZH2k"   # https://in.tradingview.com/chart/zMHJZH2k/
VIVEK_CHART_ID = "RdGFPK5y"   # https://www.tradingview.com/chart/RdGFPK5y/

OUTPUT_CONFIRMED_JSON = BUY_CONFIRMED_JSON
OUTPUT_SIGNAL_JSON   = BUY_SIGNAL_JSON


def get_engine():
    return create_engine(DATABASE_URL)


def build_url(chart_id: str, symbol: str, indian: bool = False) -> str:
    """Build a TradingView chart URL for the given layout ID and NSE symbol."""
    domain = "in.tradingview.com" if indian else "www.tradingview.com"
    encoded = urllib.parse.quote(f"NSE:{symbol}")
    return f"https://{domain}/chart/{chart_id}/?symbol={encoded}"


def get_entries(table_name: str = "confirmed_breakouts") -> list[dict]:
    """
    Fetch symbol + signal_date from the specified table.
    Returns a list of dicts ready for JSON serialisation.
    """
    engine = get_engine()

    if table_name == "confirmed_breakouts":
        query = """
            SELECT symbol, signal_date
            FROM confirmed_breakouts
            ORDER BY signal_date DESC, symbol;
        """
    else:
        # DISTINCT ON needs ORDER BY symbol first to pick the latest signal_date per symbol,
        # then we wrap in a subquery to re-sort by signal_date DESC for the final output.
        query = """
            SELECT symbol, signal_date FROM (
                SELECT DISTINCT ON (symbol) symbol, signal_date
                FROM buy_watch_list
                ORDER BY symbol, signal_date DESC
            ) sub
            ORDER BY signal_date DESC, symbol;
        """

    with engine.connect() as conn:
        rows = conn.execute(text(query)).fetchall()

    entries = []
    for idx, row in enumerate(rows, start=1):
        sym = row[0]
        sig_date = row[1]
        # sig_date may be a date object or a string
        if isinstance(sig_date, date):
            sig_date_str = sig_date.isoformat()
        else:
            sig_date_str = str(sig_date) if sig_date else None

        entries.append({
            "id":          idx,
            "symbol":      sym,
            "punit_link":  build_url(PUNIT_CHART_ID, sym, indian=True),
            "vivek_link":  build_url(VIVEK_CHART_ID, sym, indian=False),
            "signal_date": sig_date_str,
        })

    return entries


def write_json(entries: list[dict], path: str) -> None:
    with open(path, "w") as f:
        json.dump(entries, f, indent=2)
    print(f"Wrote {len(entries)} entries to {path}")


def write_to_mongo(entries: list[dict], collection_name: str) -> None:
    """Push *entries* to a MongoDB collection (clears old docs first)."""
    try:
        write_collection(collection_name, entries)
    except Exception as exc:  # noqa: BLE001
        print(f"[MongoDB] Warning: failed to write to '{collection_name}': {exc}")


def open_punit_chart(symbol: str) -> None:
    webbrowser.open_new_tab(build_url(PUNIT_CHART_ID, symbol, indian=True))


def main():
    parser = argparse.ArgumentParser(
        description="Export watch lists to JSON and open TradingView charts."
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="Number of tabs to open per batch (default: 5)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of symbols to process (applies to confirmed list only)",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Open all charts without interactive batch prompts",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Only write the JSON files, do not open any browser tabs",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=OUTPUT_CONFIRMED_JSON,
        help=f"Path for the confirmed watchlist JSON file (default: {OUTPUT_CONFIRMED_JSON})",
    )
    parser.add_argument(
        "--output-signal",
        type=str,
        default=OUTPUT_SIGNAL_JSON,
        help=f"Path for the buy-signal watchlist JSON file (default: {OUTPUT_SIGNAL_JSON})",
    )

    args = parser.parse_args()

    # -----------------------------------------------------------------------
    # Step 1: Fetch both lists from DB
    # -----------------------------------------------------------------------
    confirmed_entries = get_entries("confirmed_breakouts")
    signal_entries    = get_entries("buy_watch_list")

    # -----------------------------------------------------------------------
    # Step 2: Write both JSON files
    # -----------------------------------------------------------------------
    if confirmed_entries:
        entries_to_write = confirmed_entries[: args.limit] if args.limit else confirmed_entries
        write_json(entries_to_write, args.output)
        write_to_mongo(entries_to_write, MONGO_COLLECTION_BUY_CONFIRMED)
    else:
        print("No symbols found in 'confirmed_breakouts' — skipping buy_confirmed_watchlist.json.")
        entries_to_write = []

    if signal_entries:
        write_json(signal_entries, args.output_signal)
        write_to_mongo(signal_entries, MONGO_COLLECTION_BUY_SIGNAL)
    else:
        print("No symbols found in 'buy_watch_list' — skipping buy_signal_watchlist.json.")

    print(
        f"\nSummary:\n"
        f"  buy_confirmed_watchlist : {len(entries_to_write)} symbols\n"
        f"  buy_signal_watchlist    : {len(signal_entries)} symbols"
    )

    if args.json_only:
        return

    # -----------------------------------------------------------------------
    # Step 3: Open browser tabs (confirmed list only)
    # -----------------------------------------------------------------------
    if not entries_to_write:
        print("Nothing to open.")
        return

    symbols = [e["symbol"] for e in entries_to_write]
    total = len(symbols)
    print(f"\nOpening {total} confirmed symbols in Punit's TradingView layout ({PUNIT_CHART_ID}) …\n")

    if args.no_prompt:
        for sym in symbols:
            open_punit_chart(sym)
            time.sleep(0.2)
        print("Done.")
        return

    batch_size = args.batch_size
    for i in range(0, total, batch_size):
        batch = symbols[i : i + batch_size]
        print(f"Next batch: {', '.join(batch)}")
        user_input = input(
            f"Press Enter to open {len(batch)} chart(s) [or 'q' to quit, 'n' to skip]: "
        ).strip().lower()

        if user_input == "q":
            print("Quitting.")
            break
        elif user_input == "n":
            print("Skipping batch.")
            continue
        else:
            print(f"Opening {len(batch)} chart(s) …")
            for sym in batch:
                open_punit_chart(sym)
                time.sleep(0.3)

    print("\nDone.")


if __name__ == "__main__":
    main()
