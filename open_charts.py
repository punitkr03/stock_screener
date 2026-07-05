#!/usr/bin/env python
"""
open_charts.py

Queries the database for symbols in the watch lists (buy_watch_list or confirmed_breakouts),
writes them to a JSON file, then opens them in TradingView charts in the default browser.

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
import urllib.parse
import webbrowser
import time
from datetime import date
from sqlalchemy import create_engine, text
from config import DATABASE_URL

# ---------------------------------------------------------------------------
# TradingView chart layout IDs
# ---------------------------------------------------------------------------

PUNIT_CHART_ID = "zMHJZH2k"   # https://in.tradingview.com/chart/zMHJZH2k/
VIVEK_CHART_ID = "RdGFPK5y"   # https://www.tradingview.com/chart/RdGFPK5y/

OUTPUT_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")


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
            ORDER BY confirmation_date DESC, symbol;
        """
    else:
        query = """
            SELECT DISTINCT ON (symbol) symbol, signal_date
            FROM buy_watch_list
            ORDER BY symbol, signal_date DESC;
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


def open_punit_chart(symbol: str) -> None:
    webbrowser.open_new_tab(build_url(PUNIT_CHART_ID, symbol, indian=True))


def main():
    parser = argparse.ArgumentParser(
        description="Export watch list to JSON and open TradingView charts."
    )
    parser.add_argument(
        "--confirmed",
        action="store_true",
        help="Use confirmed_breakouts instead of buy_watch_list (default: confirmed_breakouts)",
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
        help="Maximum number of symbols to process",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Open all charts without interactive batch prompts",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Only write the JSON file, do not open any browser tabs",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=OUTPUT_JSON,
        help=f"Path for the output JSON file (default: {OUTPUT_JSON})",
    )

    args = parser.parse_args()

    # Always use confirmed_breakouts unless --confirmed flag is absent and user
    # explicitly passes --no-confirmed (kept for backward compat, currently hardcoded).
    table = "confirmed_breakouts"
    entries = get_entries(table)

    if not entries:
        print(f"No symbols found in '{table}'.")
        return

    if args.limit:
        entries = entries[: args.limit]

    # -----------------------------------------------------------------------
    # Step 1: Write JSON
    # -----------------------------------------------------------------------
    write_json(entries, args.output)

    if args.json_only:
        return

    # -----------------------------------------------------------------------
    # Step 2: Open browser tabs
    # -----------------------------------------------------------------------
    symbols = [e["symbol"] for e in entries]
    total = len(symbols)
    print(f"\nFound {total} symbols. Opening Punit's TradingView charts (layout: {PUNIT_CHART_ID}) …\n")

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
