# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASE_URL = "postgresql://postgres:password@localhost:5432/nse_scanner"

# ---------------------------------------------------------------------------
# yfinance download settings
# ---------------------------------------------------------------------------

DOWNLOAD_PERIOD = "6mo"   # history window fetched on first run
BATCH_SIZE      = 50       # symbols per yfinance batch request
AUTO_ADJUST     = False    # keep raw OHLC (splits/divs not adjusted)

# ---------------------------------------------------------------------------
# UT Bot parameters  (QuantNomad defaults)
# ---------------------------------------------------------------------------

UT_BOT_ATR_PERIOD = 1    # ATR look-back period
UT_BOT_KEY_VALUE  = 3.0  # sensitivity multiplier (higher = fewer signals)