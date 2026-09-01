"""
main.py

NSE UT Bot Scanner — CLI Entry Point
=====================================

Commands:
    python main.py fetch-symbols        Download all listed NSE equities → DB + symbols.csv
    python main.py download             Download OHLC history from yfinance → DB
    python main.py scan                 Run UT Bot scanner → signals DB + buy_signal_watchlist.json
    python main.py breakout             Run breakout confirmation → confirmed_breakouts DB
    python main.py compute-metrics      Compute fundamental valuation & solvency metrics for confirmed breakouts
    python main.py export               Export buy_confirmed_watchlist.json & buy_signal_watchlist.json
    python main.py analyze-indices      Analyze NSE indices, compute RRG / RS metrics → indices_data.json
    python main.py download-indices     Download OHLC history for NSE indices from Upstox → DB
    python main.py fetch-indices-master Fetch and extract Nifty index definitions from Upstox
    python main.py schedule             Run 4 PM IST daily scheduler daemon or print crontab line
    python main.py run                  Full daily stock pipeline (download → scan → breakout → export)
    python main.py run-all              Full refresh pipeline (analyze-indices → run)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date

PROJECT = os.path.dirname(os.path.abspath(__file__))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

PYTHON = sys.executable


def _run(script_relpath: str, extra_args: list[str] | None = None) -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT + (os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else "")
    cmd = [PYTHON, os.path.join(PROJECT, script_relpath)] + (extra_args or [])
    result = subprocess.run(cmd, cwd=PROJECT, env=env)
    return result.returncode


def cmd_fetch_symbols(_args) -> None:
    sys.exit(_run(os.path.join("fetchers", "symbols.py")))


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
    sys.exit(_run(os.path.join("fetchers", "historical_data.py"), extra))


def cmd_scan(args) -> None:
    extra = []
    if hasattr(args, "date") and args.date:
        extra += ["--date", args.date]
    if getattr(args, "symbol", None):
        extra += ["--symbol", args.symbol]
    if getattr(args, "days", None):
        extra += ["--days", str(args.days)]
    sys.exit(_run(os.path.join("scanners", "scanner.py"), extra))


def cmd_breakout(args) -> None:
    extra = []
    if getattr(args, "symbol", None):
        extra += ["--symbol", args.symbol]
    if getattr(args, "dry_run", False):
        extra.append("--dry-run")
    sys.exit(_run(os.path.join("scanners", "breakout.py"), extra))


def cmd_compute_metrics(args) -> None:
    extra = []
    if getattr(args, "symbol", None):
        extra += ["--symbol", args.symbol]
    if getattr(args, "force", False):
        extra.append("--force")
    if getattr(args, "dry_run", False):
        extra.append("--dry-run")
    sys.exit(_run(os.path.join("analytics", "processor.py"), extra))


def cmd_export(args) -> None:
    """Write buy_confirmed_watchlist.json and buy_signal_watchlist.json and optionally open charts."""
    extra = []
    if getattr(args, "json_only", False):
        extra.append("--json-only")
    if getattr(args, "batch_size", None):
        extra += ["--batch-size", str(args.batch_size)]
    if getattr(args, "limit", None):
        extra += ["--limit", str(args.limit)]
    if getattr(args, "no_prompt", False):
        extra.append("--no-prompt")
    sys.exit(_run(os.path.join("exporters", "open_charts.py"), extra))


def cmd_analyze_indices(args) -> None:
    extra = []
    if getattr(args, "no_refresh", False):
        extra.append("--no-refresh")
    if getattr(args, "benchmark", None):
        extra += ["--benchmark", args.benchmark]
    if getattr(args, "output", None):
        extra += ["--output", args.output]
    sys.exit(_run(os.path.join("scanners", "index_analyzer.py"), extra))


def cmd_download_indices(args) -> None:
    extra = []
    if getattr(args, "test", False):
        extra.append("--test")
    sys.exit(_run(os.path.join("fetchers", "index_data.py"), extra))


def cmd_fetch_indices_master(_args) -> None:
    sys.exit(_run(os.path.join("fetchers", "index_master.py")))


def cmd_schedule(args) -> None:
    extra = []
    if getattr(args, "cron", False):
        extra.append("--cron")
    if getattr(args, "run_now", False):
        extra.append("--run-now")
    sys.exit(_run(os.path.join("scheduler", "scheduler.py"), extra))


def cmd_run(_args) -> None:
    """
    Full daily stock pipeline (order matters):
      1. download        — fetch recent OHLC
      2. scan            — run UT Bot, update buy_watch_list  → writes buy_signal_watchlist.json
      3. breakout        — confirm breakouts, update confirmed_breakouts
      4. compute-metrics — compute valuation & solvency metrics for confirmed breakouts
      5. export          — write buy_confirmed_watchlist.json from up-to-date confirmed_breakouts
    """
    from exporters.open_charts import (
        OUTPUT_CONFIRMED_JSON,
        OUTPUT_SIGNAL_JSON,
        get_entries,
        write_json,
        write_to_mongo,
    )
    from config import MONGO_COLLECTION_BUY_CONFIRMED, MONGO_COLLECTION_BUY_SIGNAL

    rc = _run(os.path.join("fetchers", "historical_data.py"), ["--recent"])
    if rc != 0:
        print(f"[ERROR] historical_data fetcher failed (exit {rc})")
        sys.exit(rc)

    today = date.today().isoformat()
    rc = _run(os.path.join("scanners", "scanner.py"), ["--date", today])
    if rc != 0:
        print(f"[ERROR] scanner failed (exit {rc})")
        sys.exit(rc)

    rc = _run(os.path.join("scanners", "breakout.py"))
    if rc != 0:
        print(f"[ERROR] breakout confirmation failed (exit {rc})")
        sys.exit(rc)

    rc = _run(os.path.join("analytics", "processor.py"))
    if rc != 0:
        print(f"[ERROR] fundamental metrics calculation failed (exit {rc})")
        sys.exit(rc)

    # Export confirmed watchlist NOW — after breakout.py has refreshed confirmed_breakouts.
    print("\nExporting buy_confirmed_watchlist.json …")
    confirmed = get_entries("confirmed_breakouts")
    if confirmed:
        write_json(confirmed, OUTPUT_CONFIRMED_JSON)
        write_to_mongo(confirmed, MONGO_COLLECTION_BUY_CONFIRMED)
        print(f"  buy_confirmed_watchlist : {len(confirmed)} symbols")
    else:
        print("  confirmed_breakouts is empty — buy_confirmed_watchlist.json not written.")

    # Export buy signal watchlist — scanner.py has already updated buy_watch_list.
    print("\nExporting buy_signal_watchlist.json …")
    signals = get_entries("buy_watch_list")
    if signals:
        write_json(signals, OUTPUT_SIGNAL_JSON)
        write_to_mongo(signals, MONGO_COLLECTION_BUY_SIGNAL)
        print(f"  buy_signal_watchlist    : {len(signals)} symbols")
    else:
        print("  buy_watch_list is empty — buy_signal_watchlist.json not written.")

    sys.exit(0)


def cmd_run_all(args) -> None:
    """Full refresh pipeline: indices analysis + full stock pipeline."""
    rc = _run(os.path.join("scanners", "index_analyzer.py"))
    if rc != 0:
        print(f"[ERROR] index analysis failed (exit {rc})")
        sys.exit(rc)
def cmd_crude_oil(args) -> None:
    """Run Crude Oil Mini strategy initialization, update, live polling, or status."""
    from crude_oil import get_crude_oil_status, init_crude_oil_data, update_crude_oil_data

    if getattr(args, "status", False):
        import json
        print(json.dumps(get_crude_oil_status(), indent=2))
        return

    if getattr(args, "live", False):
        from crude_oil.daemon import run_poller
        interval = getattr(args, "interval", 60) or 60
        days = getattr(args, "days", 30) or 30
        run_poller(interval_seconds=interval, bootstrap_days=days)
        return

    if getattr(args, "update", False):
        print("Running Crude Oil incremental update...")
        status = update_crude_oil_data(recent_days=3)
    else:
        days = getattr(args, "days", 30) or 30
        print(f"Running Crude Oil initialization for {days} days...")
        status = init_crude_oil_data(days=days)

    print("\n=== Crude Oil Mini Strategy Status ===")
    print(f"Symbol:               {status.get('symbol')}")
    print(f"Total Candles Stored: {status.get('total_candles')}")
    print(f"Current Signal:       {status.get('current_signal')}")
    print(f"Buy Confirmed:        {status.get('buy_confirmed')}")
    print(f"Put-Call Ratio (PCR): {status.get('pcr')}")
    print(f"Open Interest:        {status.get('open_interest')}")



def cmd_crude_oil_status(args) -> None:
    """Print latest Crude Oil Mini signal status from DB."""
    import json
    from crude_oil import get_crude_oil_status
    print(json.dumps(get_crude_oil_status(), indent=2))


def main() -> None:
    p = argparse.ArgumentParser(
        description="NSE UT Bot Scanner & Market Analysis Platform",
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

    # compute-metrics
    cm_p = sub.add_parser("compute-metrics", help="Compute fundamental valuation & solvency metrics for confirmed breakouts")
    cm_p.add_argument(
        "--symbol",
        type=str,
        help="Compute metrics for a single specified symbol (e.g., RELIANCE)",
    )
    cm_p.add_argument(
        "--force",
        action="store_true",
        help="Force recalculation even if metrics_data already exists",
    )
    cm_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results without writing to DB",
    )

    # export
    exp_p = sub.add_parser(
        "export",
        help="Export buy_confirmed_watchlist.json and buy_signal_watchlist.json",
    )
    exp_p.add_argument(
        "--json-only",
        action="store_true",
        help="Only write JSON files, do not launch browser tabs",
    )
    exp_p.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="Number of tabs to open per batch (default: 5)",
    )
    exp_p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of symbols to open in browser",
    )
    exp_p.add_argument(
        "--no-prompt",
        action="store_true",
        help="Open all charts without interactive batch prompts",
    )

    # analyze-indices
    idx_p = sub.add_parser("analyze-indices", help="Analyze NSE indices (RRG & Relative Strength)")
    idx_p.add_argument(
        "--no-refresh",
        action="store_true",
        help="Skip recent candle download and analyze existing DB candles",
    )
    idx_p.add_argument(
        "--benchmark",
        type=str,
        default=None,
        help="Benchmark index symbol (default: NIFTY)",
    )
    idx_p.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output path for indices data JSON",
    )

    # download-indices
    idx_dl_p = sub.add_parser("download-indices", help="Download 5-year OHLCV for all NSE indices")
    idx_dl_p.add_argument(
        "--test",
        action="store_true",
        help="Test run: process only the first index",
    )

    # fetch-indices-master
    sub.add_parser("fetch-indices-master", help="Download and extract NSE index definitions master")

    # schedule
    sched_p = sub.add_parser("schedule", help="4 PM IST Daily Job Scheduler")
    sched_p.add_argument(
        "--cron",
        action="store_true",
        help="Print recommended crontab entry and exit",
    )
    sched_p.add_argument(
        "--run-now",
        action="store_true",
        help="Run pipeline immediately and exit",
    )

    # crude-oil
    co_p = sub.add_parser("crude-oil", help="Crude Oil Mini 5m Heikin Ashi + UT Bot + Breakout strategy")
    co_p.add_argument(
        "--init",
        action="store_true",
        help="Initialize 1 month (30 days) of 5-minute candles and compute strategy",
    )
    co_p.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of historical days to fetch for initialization (default: 30)",
    )
    co_p.add_argument(
        "--update",
        action="store_true",
        help="Perform incremental update with recent candles",
    )
    co_p.add_argument(
        "--live",
        action="store_true",
        help="Run continuous live polling daemon looking for new data every minute",
    )
    co_p.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Polling interval in seconds for live mode (default: 60)",
    )
    co_p.add_argument(
        "--status",
        action="store_true",
        help="Show latest strategy and breakout status from DB",
    )

    # crude-oil-status
    sub.add_parser("crude-oil-status", help="Show latest Crude Oil Mini signal, PCR, and breakout status")

    # run
    sub.add_parser("run", help="Full daily stock pipeline: download + scan + breakout + export")

    # run-all
    sub.add_parser("run-all", help="Full market pipeline: analyze-indices + run daily stock pipeline")

    args = p.parse_args()

    dispatch = {
        "fetch-symbols":        cmd_fetch_symbols,
        "download":             cmd_download,
        "scan":                 cmd_scan,
        "breakout":             cmd_breakout,
        "compute-metrics":      cmd_compute_metrics,
        "export":               cmd_export,
        "analyze-indices":      cmd_analyze_indices,
        "download-indices":     cmd_download_indices,
        "fetch-indices-master": cmd_fetch_indices_master,
        "crude-oil":            cmd_crude_oil,
        "crude-oil-status":     cmd_crude_oil_status,
        "schedule":             cmd_schedule,
        "run":                  cmd_run,
        "run-all":              cmd_run_all,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()