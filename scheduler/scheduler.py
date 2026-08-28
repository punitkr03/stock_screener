"""
scheduler.py

4:00 PM IST (10:30 UTC) Daily Job Scheduler
============================================

Runs the full pipeline every trading day at 4:00 PM IST:
    1. download_history.py  — refreshes OHLC data from yfinance
    2. scanner.py           — computes UT Bot signals

Two modes:
    A. Run as a persistent Python process (python scheduler.py)
    B. Print a cron expression and exit   (python scheduler.py --cron)

Usage:
    # Long-running daemon
    python scheduler.py

    # Just print cron line and exit
    python scheduler.py --cron

    # Run pipeline immediately once (useful for testing)
    python scheduler.py --run-now
"""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import argparse
import logging
import subprocess
from datetime import date, datetime, timezone, timedelta

import time

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Time zone helpers
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))

# 4:00 PM IST = 10:30 AM UTC
SCHEDULE_IST_HOUR   = 16  # 4 PM
SCHEDULE_IST_MINUTE = 0
SCHEDULE_UTC_HOUR   = 10
SCHEDULE_UTC_MINUTE = 30

from config import BASE_DIR

PROJECT_DIR = BASE_DIR
PYTHON      = sys.executable


# ---------------------------------------------------------------------------
# The pipeline job
# ---------------------------------------------------------------------------

def run_pipeline() -> None:
    """Run the complete daily pipeline via main.py run."""

    now_ist = datetime.now(IST)
    today   = now_ist.date()

    log.info("═" * 55)
    log.info("Pipeline starting at %s IST", now_ist.strftime("%Y-%m-%d %H:%M:%S"))
    log.info("═" * 55)

    result = subprocess.run(
        [PYTHON, os.path.join(PROJECT_DIR, "main.py"), "run"],
        capture_output=False,
        cwd=PROJECT_DIR,
    )
    if result.returncode != 0:
        log.error("Pipeline failed with exit code %d", result.returncode)
        return

    log.info("Pipeline complete for %s.", today)


# ---------------------------------------------------------------------------
# Cron helper
# ---------------------------------------------------------------------------

CRON_LINE = (
    f"30 10 * * 1-5  cd {PROJECT_DIR} && "
    f"{PYTHON} main.py schedule --run-now >> logs/scheduler.log 2>&1"
)


def print_cron() -> None:
    """Print the recommended crontab entry."""

    print("\n# Add this line to crontab (crontab -e):")
    print("# Runs at 10:30 UTC (4:00 PM IST), Monday–Friday\n")
    print(CRON_LINE)
    print()


# ---------------------------------------------------------------------------
# Daemon mode
# ---------------------------------------------------------------------------

def run_daemon() -> None:
    """Run the scheduler as a long-lived process."""
    try:
        import schedule
    except ImportError:
        log.error("The 'schedule' package is required for daemon mode. Install it via: pip install schedule")
        sys.exit(1)

    utc_time = f"{SCHEDULE_UTC_HOUR:02d}:{SCHEDULE_UTC_MINUTE:02d}"

    log.info("Scheduler started.")
    log.info(
        "Job will fire at %02d:%02d UTC (%02d:%02d IST) on weekdays.",
        SCHEDULE_UTC_HOUR, SCHEDULE_UTC_MINUTE,
        SCHEDULE_IST_HOUR, SCHEDULE_IST_MINUTE,
    )

    # schedule library uses LOCAL time — log a note if server is not UTC
    schedule.every().monday.at(utc_time).do(run_pipeline)
    schedule.every().tuesday.at(utc_time).do(run_pipeline)
    schedule.every().wednesday.at(utc_time).do(run_pipeline)
    schedule.every().thursday.at(utc_time).do(run_pipeline)
    schedule.every().friday.at(utc_time).do(run_pipeline)

    log.info("Press Ctrl+C to stop.\n")

    while True:
        schedule.run_pending()
        time.sleep(30)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NSE Scanner 4 PM IST Scheduler")
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--cron",
        action="store_true",
        help="Print crontab entry and exit",
    )
    group.add_argument(
        "--run-now",
        action="store_true",
        help="Run the pipeline immediately and exit",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.cron:
        print_cron()
    elif args.run_now:
        run_pipeline()
    else:
        run_daemon()
