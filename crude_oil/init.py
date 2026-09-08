"""
crude_oil/init.py

CLI initialization and runner script for Crude Oil Mini (CRUDEOILM).

Usage:
    python crude_oil/init.py             # Run 30-day initialization
    python crude_oil/init.py --days 30   # Specify days
    python crude_oil/init.py --update    # Incremental recent refresh
    python crude_oil/init.py --status    # Display latest DB status
"""

import argparse
import json
import logging
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import CRUDE_OIL_INIT_DAYS
from crude_oil import get_crude_oil_status, init_crude_oil_data, update_crude_oil_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Crude Oil Mini 5m Strategy Initializer & Runner")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Optional custom number of historical days of 5m candles to fetch (default: current month)",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Perform incremental update with recent candles only",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run continuous live polling daemon looking for new data",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=120,
        help="PCR polling interval in seconds for live mode (default: 120 / 2 minutes)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Display current latest signal and breakout status from database",
    )

    args = parser.parse_args()

    if args.status:
        status = get_crude_oil_status()
        print(json.dumps(status, indent=2))
        return

    if args.live:
        from crude_oil.daemon import run_poller
        run_poller(pcr_interval_seconds=args.interval)
        return

    if args.update:
        log.info("Running incremental update...")
        status = update_crude_oil_data()
    else:
        if args.days:
            log.info("Running initialization for %s days...", args.days)
            status = init_crude_oil_data(days=args.days)
        else:
            log.info("Running initialization for current month...")
            status = init_crude_oil_data()


    print("\n=== Crude Oil Mini Strategy Status ===")
    print(f"Symbol:                   {status.get('symbol')}")
    print(f"Total Candles Stored:     {status.get('total_candles')}")
    print(f"Current Signal:           {status.get('current_signal')}")
    print(f"Buy Confirmed:            {status.get('buy_confirmed')}")
    print(f"Sell Confirmed:           {status.get('sell_confirmed')}")
    print(f"Put-Call Ratio (PCR):     {status.get('pcr')}")
    print(f"Open Interest:            {status.get('open_interest')}")


    latest = status.get("latest_candle")
    if latest:
        print(f"Latest Candle Time:       {latest.get('timestamp')}")
        print(f"Close:                    {latest.get('close')}")
        print(f"HA Close:                 {latest.get('ha_close')}")
        print(f"Trailing Stop:            {latest.get('trailing_stop')}")

    last_act = status.get("last_actionable_signal")
    if last_act:
        print(f"Last Actionable Signal:   {last_act.get('signal')} at {last_act.get('timestamp')}")


if __name__ == "__main__":
    main()
