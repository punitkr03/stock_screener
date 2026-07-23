# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASE_URL = "postgresql://postgres:password@localhost:5432/nse_scanner"

# ---------------------------------------------------------------------------
# Upstox download settings
# ---------------------------------------------------------------------------

DOWNLOAD_PERIOD = "2y"    # history window — translates to a from_date offset at runtime

# URL for the Upstox NSE master instruments JSON (gzip-compressed).
# This file maps trading symbols → instrument_key (NSE_EQ|ISIN format).
UPSTOX_INSTRUMENTS_URL   = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

# Local cache file for the instruments map (refreshed if older than 24 h)
UPSTOX_INSTRUMENTS_CACHE = "upstox_instruments.json"

# Polite delay between successive Upstox API calls (seconds)
UPSTOX_DELAY_SECS = 0.01   # ≤ 5 requests/second  (1 / 0.2 = 5 req/s)

# ---------------------------------------------------------------------------
# UT Bot parameters  (QuantNomad defaults)
# ---------------------------------------------------------------------------

UT_BOT_ATR_PERIOD = 55   # ATR look-back period
UT_BOT_KEY_VALUE  = 1.0  # sensitivity multiplier