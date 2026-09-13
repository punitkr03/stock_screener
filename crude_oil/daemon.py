"""
crude_oil/daemon.py

Continuous Put-Call Ratio (PCR) polling daemon for Crude Oil Mini (CRUDEOILM).
Features:
  1. Market Hours Restriction:
     - MCX Crude Oil operates Monday to Friday from 09:00 AM to 11:30 PM (23:30) IST.
     - Polling pauses during weekends and non-market hours.
  2. Dedicated 3-Minute Put-Call Ratio (PCR) Poller:
     - Calculates and stores PCR records in crude_oil_pcr_data every 180 seconds.
     - Checks and dispatches Telegram state-change alerts.
     - Fast 15-second retry on API rate limit or network failures.

Usage:
    python crude_oil/daemon.py
    python crude_oil/daemon.py --ignore-market-hours
"""

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import CRUDE_OIL_PCR_INTERVAL_SECONDS
from crude_oil import (
    is_crude_oil_market_open,
    update_crude_oil_pcr,
)
from crude_oil.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("crude_oil_daemon")

_running = True
IST = ZoneInfo("Asia/Kolkata")


def _signal_handler(sig, frame):
    global _running
    log.info("Shutdown signal received. Stopping Crude Oil PCR daemon...")
    _running = False


def run_poller(
    pcr_interval_seconds: int = CRUDE_OIL_PCR_INTERVAL_SECONDS,
    tick_interval_seconds: int = 2,
    ignore_market_hours: bool = False,
) -> None:
    """
    Run dedicated PCR polling loop:
      - Enforces MCX market window (09:00 - 23:30 IST, Mon-Fri).
      - Calculates PCR every `pcr_interval_seconds` (default: 180s = 3min).
      - Saves records to DB and triggers Telegram alerts on state transitions.
      - Fast 15s retry on failure.
    """
    global _running
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    init_db()

    log.info("═" * 72)
    log.info("Crude Oil Mini PCR Polling Daemon started")
    log.info("  • Market Hours:      Mon - Fri, 09:00 AM - 11:30 PM IST")
    log.info("  • PCR Polling:       Every %s seconds (%s min)", pcr_interval_seconds, pcr_interval_seconds // 60)
    if ignore_market_hours:
        log.warning("  • Market Hours Check: BYPASSED (--ignore-market-hours enabled)")
    log.info("Press Ctrl+C to stop.")
    log.info("═" * 72)

    last_pcr_time = 0.0
    market_closed_logged = False

    while _running:
        try:
            now_ist = datetime.now(IST)
            now_epoch = time.time()

            # -----------------------------------------------------------------
            # 1. Market Hours Check (09:00 - 23:30 IST, Mon-Fri)
            # -----------------------------------------------------------------
            market_open = is_crude_oil_market_open(now_ist, grace_minutes=5) or ignore_market_hours

            if not market_open:
                if not market_closed_logged:
                    log.info(
                        "[%s IST] MCX Crude Oil market is closed (Open: Mon-Fri 09:00 - 23:30 IST). Polling paused.",
                        now_ist.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                    market_closed_logged = True

                # Sleep small slices for shutdown responsiveness
                for _ in range(tick_interval_seconds):
                    if not _running:
                        break
                    time.sleep(1)
                continue

            if market_closed_logged:
                log.info(
                    "[%s IST] MCX Crude Oil market is OPEN. Resuming live PCR polling.",
                    now_ist.strftime("%Y-%m-%d %H:%M:%S"),
                )
                market_closed_logged = False

            # -----------------------------------------------------------------
            # 2. PCR Polling & Notification Engine
            # -----------------------------------------------------------------
            if now_epoch - last_pcr_time >= pcr_interval_seconds:
                try:
                    pcr_record = update_crude_oil_pcr(
                        force=True,  # Daemon schedule governs updates
                        ignore_market_hours=ignore_market_hours,
                    )
                    if pcr_record:
                        last_pcr_time = now_epoch
                        log.info(
                            "[%s IST] [PCR Saved] PCR: %s | PE OI: %s | CE OI: %s",
                            now_ist.strftime("%H:%M:%S"),
                            pcr_record.get("pcr"),
                            pcr_record.get("pe_oi"),
                            pcr_record.get("ce_oi"),
                        )
                    else:
                        log.warning(
                            "[%s IST] PCR update returned None. Retrying in 15s...",
                            now_ist.strftime("%H:%M:%S"),
                        )
                        last_pcr_time = now_epoch - pcr_interval_seconds + 15
                except Exception as pcr_exc:
                    log.error("Error during PCR update: %s. Retrying in 15s...", pcr_exc)
                    last_pcr_time = now_epoch - pcr_interval_seconds + 15

        except Exception as exc:
            log.error("Unhandled error during Crude Oil daemon loop: %s", exc, exc_info=True)

        # Sleep in small slices for responsive shutdown
        for _ in range(tick_interval_seconds):
            if not _running:
                break
            time.sleep(1)

    log.info("Crude Oil PCR polling daemon stopped.")


def main():
    parser = argparse.ArgumentParser(description="Crude Oil Mini Live PCR Polling Daemon")
    parser.add_argument(
        "--pcr-interval",
        "--interval",
        dest="pcr_interval",
        type=int,
        default=CRUDE_OIL_PCR_INTERVAL_SECONDS,
        help="PCR polling interval in seconds (default: 180 / 3 minutes)",
    )
    parser.add_argument(
        "--buffer",
        type=int,
        default=5,
        help="Deprecated/Ignored compatibility argument",
    )
    parser.add_argument(
        "--retry-interval",
        type=int,
        default=15,
        help="Deprecated/Ignored compatibility argument",
    )
    parser.add_argument(
        "--ignore-market-hours",
        action="store_true",
        help="Bypass market hours check (runs polling even outside 09:00 - 23:30 IST)",
    )
    args = parser.parse_args()

    run_poller(
        pcr_interval_seconds=args.pcr_interval,
        ignore_market_hours=args.ignore_market_hours,
    )


if __name__ == "__main__":
    main()

