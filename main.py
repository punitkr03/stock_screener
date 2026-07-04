"""
main.py

NSE UT Bot Scanner — CLI Entry Point
=====================================

Commands:
    python main.py fetch-symbols    Download all listed NSE equities → symbols.csv
    python main.py download         Download OHLC history from yfinance → DB
    python main.py scan             Run UT Bot scanner → signals CSV + DB
    python main.py run              download + scan (full pipeline, once)
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
    sys.exit(_run("download_history.py", extra))


def cmd_scan(args) -> None:
    extra = []
    if hasattr(args, "date") and args.date:
        extra += ["--date", args.date]
    if getattr(args, "csv_only", False):
        extra.append("--csv-only")
    sys.exit(_run("scanner.py", extra))


def cmd_run(args) -> None:
    """Full pipeline: download then scan."""

    rc = _run("download_history.py")
    if rc != 0:
        print(f"[ERROR] download_history.py failed (exit {rc})")
        sys.exit(rc)

    today = date.today().isoformat()
    rc = _run("scanner.py", ["--date", today])
    sys.exit(rc)



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

    # scan
    scan_p = sub.add_parser("scan", help="Run UT Bot scanner")
    scan_p.add_argument("--date", help="Scan date YYYY-MM-DD (default: today)")
    scan_p.add_argument("--csv-only", action="store_true", help="Skip DB writes")

    # run
    sub.add_parser("run", help="Download + scan (full pipeline)")

    args = p.parse_args()

    dispatch = {
        "fetch-symbols": cmd_fetch_symbols,
        "download":      cmd_download,
        "scan":          cmd_scan,
        "run":           cmd_run,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()