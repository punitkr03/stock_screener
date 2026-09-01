# ---------------------------------------------------------------------------
# Environment & Paths
# ---------------------------------------------------------------------------

import os
import sys
from dotenv import load_dotenv

# Base Project Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# Data File Paths
SYMBOLS_CSV        = os.getenv("SYMBOLS_CSV", os.path.join(DATA_DIR, "symbols.csv"))
NIFTY_INDICES_JSON = os.getenv("NIFTY_INDICES_JSON", os.path.join(DATA_DIR, "nifty_indices.json"))
INDICES_DATA_JSON  = os.getenv("INDICES_DATA_JSON", os.path.join(DATA_DIR, "indices_data.json"))
BUY_CONFIRMED_JSON = os.getenv("BUY_CONFIRMED_JSON", os.path.join(DATA_DIR, "buy_confirmed_watchlist.json"))
BUY_SIGNAL_JSON    = os.getenv("BUY_SIGNAL_JSON", os.path.join(DATA_DIR, "buy_signal_watchlist.json"))

# Load variables from .env (no-op if the file doesn't exist)
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ---------------------------------------------------------------------------
# PostgreSQL Database
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:password@localhost:5432/nse_scanner",
)

# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB  = os.getenv("MONGODB_DB", "nse_scanner")

# Collection names
MONGO_COLLECTION_BUY_CONFIRMED = "buy_confirmed_data"
MONGO_COLLECTION_BUY_SIGNAL    = "buy_signal_data"
MONGO_COLLECTION_INDICES       = "indices_data"

# ---------------------------------------------------------------------------
# yfinance download settings
# ---------------------------------------------------------------------------

DOWNLOAD_PERIOD = "2y"    # history window — 2 years gives ATR(55) enough warmup to match TradingView
BATCH_SIZE      = 50       # symbols per yfinance batch request
AUTO_ADJUST     = False    # keep raw OHLC (splits/divs not adjusted)

# ---------------------------------------------------------------------------
# UT Bot parameters  (QuantNomad defaults)
# ---------------------------------------------------------------------------

UT_BOT_ATR_PERIOD = 55   # ATR look-back period
UT_BOT_KEY_VALUE  = 1.0  # sensitivity multiplier

# ---------------------------------------------------------------------------
# Upstox API
# ---------------------------------------------------------------------------

UPSTOX_AUTH_TOKEN = os.getenv("UPSTOX_AUTH_TOKEN", "")

# Crude Oil Mini settings
# ---------------------------------------------------------------------------
CRUDE_OIL_SYMBOL              = "CRUDEOILM"
CRUDE_OIL_CANDLE_INTERVAL     = "5"     # 5-minute candles
CRUDE_OIL_INIT_DAYS           = 30    # 1 month (30 days) history seed
CRUDE_OIL_UT_BOT_ATR_PERIOD   = int(os.getenv("CRUDE_OIL_UT_BOT_ATR_PERIOD", "55"))    # TradingView UT Bot ATR period: 55
CRUDE_OIL_UT_BOT_KEY_VALUE    = float(os.getenv("CRUDE_OIL_UT_BOT_KEY_VALUE", "1.0"))  # TradingView UT Bot Key Value: 1.0




# ---------------------------------------------------------------------------
# Index symbols helper
# ---------------------------------------------------------------------------


def load_index_symbols() -> set[str]:
    """
    Load all index symbols and trading symbols from nifty_indices.json.
    Returns a set of normalized uppercase symbols (e.g. {'NIFTY 50', 'BANKNIFTY', ...}).
    """
    symbols = set()
    if os.path.exists(NIFTY_INDICES_JSON):
        try:
            import json
            with open(NIFTY_INDICES_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data:
                    if isinstance(item, dict):
                        ts = item.get("trading_symbol")
                        name = item.get("name")
                        if ts:
                            symbols.add(ts.strip().upper())
                        if name:
                            symbols.add(name.strip().upper())
        except Exception:
            pass
    return symbols