"""
main.py

NSE UT Bot Scanner — CLI Entry Point
=====================================

Commands:
    python main.py fetch-symbols    Download all listed NSE equities → DB + symbols.csv
    python main.py download         Download OHLC history from yfinance → DB
    python main.py scan             Run UT Bot scanner → signals DB + buy_signal_watchlist.json
    python main.py breakout         Run breakout confirmation → confirmed_breakouts DB
    python main.py export           Export buy_confirmed_watchlist.json & buy_signal_watchlist.json
    python main.py run              Full daily pipeline:
                                      download → scan (buy_signal_watchlist.json)
                                      → breakout → export buy_confirmed_watchlist.json
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import os
from datetime import date, datetime

PROJECT = os.path.dirname(os.path.abspath(__file__))
PYTHON  = sys.executable


def _run(script: str, extra_args: list[str] | None = None) -> int:
    cmd = [PYTHON, os.path.join(PROJECT, script)] + (extra_args or [])
    result = subprocess.run(cmd, cwd=PROJECT)
    return result.returncode


def cmd_fetch_symbols(_args) -> None:
    sys.exit(_run("fetch_symbols.py"))


def cmd_download(args) -> None:
    extra = []
    if getattr(args, "test", False):
        extra.append("--test")
    if getattr(args, "symbol", None):
        extra += ["--symbol", args.symbol]
    if getattr(args, "recent", False):
        extra.append("--recent")
    if getattr(args, "period", None):
        extra += ["--period", args.period]
    sys.exit(_run("download_history.py", extra))


def cmd_scan(args) -> None:
    extra = []
    if hasattr(args, "date") and args.date:
        extra += ["--date", args.date]
    if getattr(args, "symbol", None):
        extra += ["--symbol", args.symbol]
    if getattr(args, "days", None):
        extra += ["--days", str(args.days)]
    sys.exit(_run("scanner.py", extra))


def cmd_breakout(args) -> None:
    extra = []
    if getattr(args, "symbol", None):
        extra += ["--symbol", args.symbol]
    if getattr(args, "dry_run", False):
        extra.append("--dry-run")
    sys.exit(_run("breakout.py", extra))


def cmd_export(_args) -> None:
    """Write buy_confirmed_watchlist.json and buy_signal_watchlist.json."""
    sys.exit(_run("open_charts.py", ["--json-only"]))


def cmd_run(args) -> None:
    """
    Full daily pipeline (order matters):
      1. download  — fetch recent OHLC
      2. scan      — run UT Bot, update buy_watch_list  → writes buy_signal_watchlist.json
      3. breakout  — confirm breakouts, update confirmed_breakouts
      4. export    — write buy_confirmed_watchlist.json from up-to-date confirmed_breakouts
    """
    import sys as _sys
    import os as _os
    _sys.path.insert(0, PROJECT)
    from open_charts import get_entries, write_json, OUTPUT_CONFIRMED_JSON

    rc = _run("download_history.py", ["--recent"])
    if rc != 0:
        print(f"[ERROR] download_history.py failed (exit {rc})")
        sys.exit(rc)

    today = date.today().isoformat()
    rc = _run("scanner.py", ["--date", today])
    if rc != 0:
        print(f"[ERROR] scanner.py failed (exit {rc})")
        sys.exit(rc)

    rc = _run("breakout.py")
    if rc != 0:
        print(f"[ERROR] breakout.py failed (exit {rc})")
        sys.exit(rc)

    # Export confirmed watchlist NOW — after breakout.py has refreshed confirmed_breakouts.
    print("\nExporting buy_confirmed_watchlist.json …")
    confirmed = get_entries("confirmed_breakouts")
    if confirmed:
        write_json(confirmed, OUTPUT_CONFIRMED_JSON)
        print(f"  buy_confirmed_watchlist : {len(confirmed)} symbols")
    else:
        print("  confirmed_breakouts is empty — buy_confirmed_watchlist.json not written.")

    sys.exit(0)



def main() -> None:
    p = argparse.ArgumentParser(
        description="NSE UT Bot Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="command", required=True)

    # fetch-symbols
    sub.add_parser("fetch-symbols", help="Download all listed NSE equities to symbols.csv")

    # download
    dl_p = sub.add_parser("download", help="Download OHLC history to PostgreSQL")
    dl_p.add_argument(
        "--test",
        action="store_true",
        help="Test run: download only the first symbol",
    )
    dl_p.add_argument(
        "--symbol",
        type=str,
        help="Download history for a single specified symbol (e.g., RELIANCE)",
    )
    dl_p.add_argument(
        "--recent",
        action="store_true",
        help="Daily mode: fetch only the last 5 days instead of the full history",
    )
    dl_p.add_argument(
        "--period",
        type=str,
        help="Override download period (e.g. 5d, 1mo, 2y)",
    )

    # scan
    scan_p = sub.add_parser("scan", help="Run UT Bot scanner")
    scan_p.add_argument("--date", help="Scan date YYYY-MM-DD (default: today)")
    scan_p.add_argument(
        "--symbol",
        type=str,
        help="Scan only a single symbol (e.g., RELIANCE)",
    )
    scan_p.add_argument(
        "--days",
        type=int,
        default=1,
        help="Number of past days/candles to scan for signals (default: 1)",
    )

    # breakout
    bo_p = sub.add_parser("breakout", help="Run breakout confirmation → buy_watch_list")
    bo_p.add_argument(
        "--symbol",
        type=str,
        help="Evaluate only a single symbol (e.g., RELIANCE)",
    )
    bo_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results without writing to DB",
    )

    # export
    sub.add_parser(
        "export",
        help="Export buy_confirmed_watchlist.json and buy_signal_watchlist.json",
    )

    # run
    sub.add_parser("run", help="Full daily pipeline: download + scan + breakout + export")

    args = p.parse_args()

    dispatch = {
        "fetch-symbols": cmd_fetch_symbols,
        "download":      cmd_download,
        "scan":          cmd_scan,
        "breakout":      cmd_breakout,
        "export":        cmd_export,
        "run":           cmd_run,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()