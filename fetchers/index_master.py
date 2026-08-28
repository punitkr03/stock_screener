import gzip
import json
import os
import sys
from pathlib import Path

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import requests
from config import DATA_DIR, NIFTY_INDICES_JSON

INSTRUMENT_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
GZ_FILE = Path(DATA_DIR) / "NSE.json.gz"
OUTPUT_FILE = Path(NIFTY_INDICES_JSON)


def download_file():
    if GZ_FILE.exists():
        print(f"{GZ_FILE} already exists. Skipping download.")
        return

    print("Downloading NSE instrument master...")

    with requests.get(INSTRUMENT_URL, stream=True, timeout=60) as r:
        r.raise_for_status()

        with open(GZ_FILE, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

    print(f"Downloaded to {GZ_FILE}")


def load_instruments():
    with gzip.open(GZ_FILE, "rt", encoding="utf-8") as f:
        return json.load(f)


def filter_nifty_indices(instruments):
    return sorted(
        [
            instrument
            for instrument in instruments
            if instrument.get("segment") == "NSE_INDEX"
            and "nifty" in instrument.get("name", "").lower()
        ],
        key=lambda x: x["name"],
    )


def main():
    download_file()

    instruments = load_instruments()

    nifty_indices = filter_nifty_indices(instruments)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(nifty_indices, f, indent=2)

    print(f"Found {len(nifty_indices)} Nifty indices.")
    print(f"Saved to {OUTPUT_FILE.resolve()}")


if __name__ == "__main__":
    main()