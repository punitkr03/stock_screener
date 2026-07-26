# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

import os
from dotenv import load_dotenv

# Load variables from .env (no-op if the file doesn't exist)
load_dotenv()

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