# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASE_URL = "postgresql://postgres:password@localhost:5432/nse_scanner"

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