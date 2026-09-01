"""
crude_oil/daemon.py

Continuous polling daemon for Crude Oil Mini (CRUDEOILM).
Polls Upstox API every minute (or configurable interval) for new 5m candle data,
recalculates Heikin Ashi, UT Bot, Breakout confirmation, and PCR, and updates PostgreSQL.

Usage:
    python crude_oil/daemon.py
    python crude_oil/daemon.py --interval 60
    python main.py crude-oil --live
"""

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crude_oil import get_crude_oil_status, init_crude_oil_data, update_crude_oil_data
from crude_oil.db import init_db, load_candles_from_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("crude_oil_daemon")

_running = True


def _signal_handler(sig, frame):
    global _running
    log.info("Shutdown signal received. Stopping Crude Oil polling daemon...")
    _running = False


def run_poller(interval_seconds: int = 60, bootstrap_days: int = 30) -> None:
    """Run persistent polling loop fetching data every interval_seconds."""
    global _running
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    init_db()
    existing = load_candles_from_db(limit=1)
    if existing.empty:
        log.info("No candle history found in DB. Bootstrapping with %s days...", bootstrap_days)
        init_crude_oil_data(days=bootstrap_days)

    log.info("═" * 65)
    log.info("Crude Oil Mini Live Polling Daemon started (interval: %ss)", interval_seconds)
    log.info("Press Ctrl+C to stop.")
    log.info("═" * 65)

    while _running:
        try:
            start_t = time.time()
            status = update_crude_oil_data(recent_days=2)

            latest = status.get("latest_candle") or {}
            ts = latest.get("timestamp", "N/A")
            close = latest.get("close", "N/A")
            sig = status.get("current_signal", "NONE")
            confirmed = status.get("buy_confirmed", False)
            pcr = status.get("pcr", "N/A")
            oi = status.get("open_interest", "N/A")

            log.info(
                "[%s] Candle: %s | Close: %s | Signal: %s | Confirmed: %s | PCR: %s | OI: %s",
                datetime.now().strftime("%H:%M:%S"),
                ts,
                close,
                sig,
                confirmed,
                pcr,
                oi,
            )

        except Exception as exc:
            log.error("Error during Crude Oil poll cycle: %s", exc, exc_info=True)

        # Sleep remaining time
        elapsed = time.time() - start_t
        sleep_time = max(1.0, interval_seconds - elapsed)
        for _ in range(int(sleep_time)):
            if not _running:
                break
            time.sleep(1)

    log.info("Crude Oil polling daemon stopped.")


def main():
    parser = argparse.ArgumentParser(description="Crude Oil Mini Live Polling Daemon")
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Polling interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--bootstrap-days",
        type=int,
        default=30,
        help="Days of history to initialize if DB is empty (default: 30)",
    )
    args = parser.parse_args()

    run_poller(interval_seconds=args.interval, bootstrap_days=args.bootstrap_days)


if __name__ == "__main__":
    main()
