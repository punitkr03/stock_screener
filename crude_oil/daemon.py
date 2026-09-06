"""
crude_oil/daemon.py

Continuous polling daemon for Crude Oil Mini (CRUDEOILM).
Features:
  1. Market Hours Restriction:
     - MCX Crude Oil operates Monday to Friday from 09:00 AM to 11:30 PM (23:30) IST.
     - Polling and PCR updates pause during weekends and non-market hours.
  2. Independent 3-Minute Put-Call Ratio (PCR) Poller:
     - Calculates and stores PCR records in crude_oil_pcr_data every 180 seconds.
  3. Clock-Aligned 5-Minute Candle Updates with DB Confirmation & Fallback:
     - Triggers at wall-clock 5-minute boundaries (XX:00, XX:05, ... + 5s buffer).
     - Checks PostgreSQL for update confirmation of the newly finalized 5m candle.
     - If the candle is not yet confirmed in DB, retries every 15s until confirmed.

Usage:
    python crude_oil/daemon.py
    python crude_oil/daemon.py --ignore-market-hours
    python main.py crude-oil --live
"""

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crude_oil import (
    get_crude_oil_status,
    get_latest_candle_timestamp,
    init_crude_oil_data,
    is_crude_oil_market_open,
    update_crude_oil_data,
    update_crude_oil_pcr,
)
from crude_oil.db import init_db, load_candles_from_db

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
    log.info("Shutdown signal received. Stopping Crude Oil polling daemon...")
    _running = False


def run_poller(
    pcr_interval_seconds: int = 180,
    candle_buffer_seconds: int = 5,
    retry_interval_seconds: int = 15,
    tick_interval_seconds: int = 5,
    ignore_market_hours: bool = False,
) -> None:
    """
    Run persistent polling loop:
      - Enforces MCX market window (09:00 - 23:30 IST, Mon-Fri).
      - Calculates PCR every `pcr_interval_seconds` (default: 180s = 3min).
      - Updates 5m candles on 5-minute clock boundaries with DB confirmation and fallback retries.
    """
    global _running
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    init_db()
    existing = load_candles_from_db(limit=1)
    if existing.empty:
        log.info("No candle history found in DB. Initializing current month data...")
        init_crude_oil_data()

    log.info("═" * 72)
    log.info("Crude Oil Mini Live Polling Daemon started")
    log.info("  • Market Hours:      Mon - Fri, 09:00 AM - 11:30 PM IST")
    log.info("  • PCR Polling:       Every %s seconds (%s min)", pcr_interval_seconds, pcr_interval_seconds // 60)
    log.info("  • Candle Trigger:    Clock-aligned 5-min boundaries (XX:00, XX:05, ... + %ss)", candle_buffer_seconds)
    log.info("  • DB Confirmation:   Verified with %ss fallback retries", retry_interval_seconds)
    if ignore_market_hours:
        log.warning("  • Market Hours Check: BYPASSED (--ignore-market-hours enabled)")
    log.info("Press Ctrl+C to stop.")
    log.info("═" * 72)

    last_pcr_time = 0.0
    last_candle_attempt_time = 0.0
    last_confirmed_bucket = None
    market_closed_logged = False

    while _running:
        try:
            now_ist = datetime.now(IST)
            now_utc = datetime.now(timezone.utc)
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
                    "[%s IST] MCX Crude Oil market is OPEN. Resuming live data polling.",
                    now_ist.strftime("%Y-%m-%d %H:%M:%S"),
                )
                market_closed_logged = False

            # -----------------------------------------------------------------
            # 2. Independent 3-Minute PCR Polling
            # -----------------------------------------------------------------
            if now_epoch - last_pcr_time >= pcr_interval_seconds:
                try:
                    pcr_record = update_crude_oil_pcr()
                    last_pcr_time = now_epoch
                    if pcr_record:
                        log.info(
                            "[%s IST] [PCR Update] PCR: %s | PE OI: %s | CE OI: %s",
                            now_ist.strftime("%H:%M:%S"),
                            pcr_record.get("pcr"),
                            pcr_record.get("pe_oi"),
                            pcr_record.get("ce_oi"),
                        )
                except Exception as pcr_exc:
                    log.error("Error during PCR update: %s", pcr_exc)
                    # Retry in 30s on failure instead of waiting full 3m
                    last_pcr_time = now_epoch - pcr_interval_seconds + 30

            # -----------------------------------------------------------------
            # 3. 5-Minute Candle Update & DB Confirmation Fallback
            # -----------------------------------------------------------------
            # Identify current 5-minute bucket (e.g. 11:55:00 IST)
            current_bucket_ist = now_ist.replace(
                minute=(now_ist.minute // 5) * 5,
                second=0,
                microsecond=0,
            )
            # The candle that just completed as this bucket began started at (current_bucket - 5 min)
            expected_candle_ts_ist = current_bucket_ist - timedelta(minutes=5)
            expected_candle_ts_utc = expected_candle_ts_ist.astimezone(timezone.utc)

            # Check if this bucket needs sync / retry:
            # 1. Bucket not confirmed yet
            # 2. Reached buffer seconds (e.g. >= 5s after 5-min mark)
            # 3. Enough time elapsed since last attempt (retry interval)
            needs_sync = (
                current_bucket_ist != last_confirmed_bucket
                and now_ist.second >= candle_buffer_seconds
                and (now_epoch - last_candle_attempt_time >= retry_interval_seconds)
            )

            if needs_sync:
                last_candle_attempt_time = now_epoch
                log.info(
                    "[%s IST] 5-minute candle boundary (Bucket: %s IST). Syncing candle (Expected: %s IST)...",
                    now_ist.strftime("%H:%M:%S"),
                    current_bucket_ist.strftime("%H:%M"),
                    expected_candle_ts_ist.strftime("%H:%M"),
                )

                try:
                    status = update_crude_oil_data()

                    # DB Update Confirmation:
                    latest_db_ts = get_latest_candle_timestamp()

                    # At market open (09:00 - 09:05 IST), no candle has closed yet today
                    is_market_open_startup = now_ist.time() < dtime(9, 5, 0)

                    if is_market_open_startup or (
                        latest_db_ts is not None and latest_db_ts >= expected_candle_ts_utc
                    ):
                        # Successfully confirmed in DB
                        last_confirmed_bucket = current_bucket_ist
                        latest = status.get("latest_candle") or {}
                        ts = latest.get("candle_start_time") or latest.get("timestamp", "N/A")
                        close = latest.get("close", "N/A")
                        sig = status.get("current_signal", "NONE")
                        buy_conf = status.get("buy_confirmed", False)
                        sell_conf = status.get("sell_confirmed", False)
                        pcr = status.get("pcr", "N/A")
                        oi = status.get("open_interest", "N/A")

                        log.info(
                            "[%s IST] [Candle Confirmed in DB] Candle: %s | Close: %s | Signal: %s | BuyConf: %s | SellConf: %s | PCR: %s | OI: %s",
                            now_ist.strftime("%H:%M:%S"),
                            ts,
                            close,
                            sig,
                            buy_conf,
                            sell_conf,
                            pcr,
                            oi,
                        )
                    else:
                        # Fallback: expected candle not yet in DB, retry on next tick
                        latest_db_str = (
                            latest_db_ts.astimezone(IST).strftime("%H:%M:%S")
                            if latest_db_ts
                            else "None"
                        )
                        log.warning(
                            "[%s IST] [Candle Fallback] Expected candle (%s IST) not yet in DB (Latest in DB: %s IST). Will retry in %ss...",
                            now_ist.strftime("%H:%M:%S"),
                            expected_candle_ts_ist.strftime("%H:%M"),
                            latest_db_str,
                            retry_interval_seconds,
                        )

                except Exception as candle_exc:
                    log.error("Error during candle sync: %s", candle_exc)

        except Exception as exc:
            log.error("Unhandled error during Crude Oil daemon loop: %s", exc, exc_info=True)

        # Sleep in small slices for responsive shutdown
        for _ in range(tick_interval_seconds):
            if not _running:
                break
            time.sleep(1)

    log.info("Crude Oil polling daemon stopped.")


def main():
    parser = argparse.ArgumentParser(description="Crude Oil Mini Live Polling Daemon")
    parser.add_argument(
        "--pcr-interval",
        "--interval",
        dest="pcr_interval",
        type=int,
        default=180,
        help="PCR polling interval in seconds (default: 180 / 3 minutes)",
    )
    parser.add_argument(
        "--buffer",
        type=int,
        default=5,
        help="Seconds buffer after 5-minute mark to trigger candle sync (default: 5)",
    )
    parser.add_argument(
        "--retry-interval",
        type=int,
        default=15,
        help="Fallback retry interval in seconds if expected candle not confirmed in DB (default: 15)",
    )
    parser.add_argument(
        "--ignore-market-hours",
        action="store_true",
        help="Bypass market hours check (runs polling even outside 09:00 - 23:30 IST)",
    )
    args = parser.parse_args()

    run_poller(
        pcr_interval_seconds=args.pcr_interval,
        candle_buffer_seconds=args.buffer,
        retry_interval_seconds=args.retry_interval,
        ignore_market_hours=args.ignore_market_hours,
    )


if __name__ == "__main__":
    main()

