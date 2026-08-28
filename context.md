# Project Context & Architecture Guide

## 📌 Project Overview
**Stock Screener & Short-Term Trading Engine** is an automated analysis and signal-generation platform for National Stock Exchange of India (NSE) equities and sector indices. It automates:
1. **Universe & Historical Data Ingestion**: Pulls active NSE equities (from NSE India archives) and historical OHLCV data from Yahoo Finance and Upstox.
2. **Technical Indicator Engine**: Computes Heikin Ashi candles and the QuantNomad UT Bot (ATR Trailing Stop + Key Value ATR sensitivity).
3. **Screening & Breakout Strategies**:
   - Daily UT Bot BUY/SELL screening on all listed equities.
   - Breakout confirmation engine with exhaustion filter (<10% range) and strict/deferred follow-through logic.
   - Relative Rotation Graphs (RRG) & sector/index momentum analyzer.
4. **Export & Visualization**: Exports structured watchlists to JSON and MongoDB, with direct batch launch to TradingView layout charts.
5. **REST API & Scheduling**: Automated 4:00 PM IST daily cron/daemon execution and FastAPI backend for external dashboard triggers.

---

## 📁 Directory Structure

```
short-term-trading/
├── main.py                        # Unified root CLI entrypoint for all pipelines and commands
├── config.py                      # Global configuration, directory paths, DB & model params
├── context.md                     # Complete project context and architectural reference
├── requirements.txt               # Python package dependencies
├── docker-compose.yml             # Local PostgreSQL & MongoDB container setup
├── .env                           # Local environment variables & secrets (git-ignored)
├── .gitignore                     # Git ignore rules for virtualenvs, caches, logs, and data
│
├── data/                          # Master lists, input/output data artifacts & caches
│   ├── symbols.csv                # List of active NSE equity symbols and names
│   ├── nifty_indices.json         # Extracted Nifty index definitions master
│   ├── indices_data.json          # Processed RRG / Relative Strength metrics for indices
│   ├── buy_signal_watchlist.json  # Exported watch list with active UT Bot BUY signals
│   ├── buy_confirmed_watchlist.json # Exported confirmed breakout symbols
│   └── zdata.py                   # Reference/test indices dataset
│
├── fetchers/                      # Data acquisition & ingestion modules
│   ├── __init__.py
│   ├── symbols.py                 # Downloads NSE equity universe from official NSE archives
│   ├── historical_data.py         # Downloads daily OHLCV from Yahoo Finance into PostgreSQL
│   ├── index_data.py              # Downloads 5-year OHLCV for indices via Upstox API
│   └── index_master.py            # Downloads and extracts NSE index definitions from Upstox master
│
├── indicators/                    # Technical indicator calculation engine
│   ├── __init__.py
│   ├── heikin_ashi.py             # Heikin Ashi candle calculation
│   └── ut_bot.py                  # UT Bot (Wilder's ATR Trailing Stop + EMA signal line)
│
├── scanners/                      # Screening, signal detection & market analysis
│   ├── __init__.py
│   ├── scanner.py                 # Daily UT Bot scanner (persists signals to DB & watchlists)
│   ├── breakout.py                # Breakout confirmation engine & exhaustion filtering
│   └── index_analyzer.py          # Sector & index RRG (Relative Rotation Graph) analyzer
│
├── exporters/                     # Watchlist exporters & chart launchers
│   ├── __init__.py
│   └── open_charts.py             # Exports JSON watchlists, MongoDB sync & opens TradingView tabs
│
├── scheduler/                     # Automation & scheduled runs
│   ├── __init__.py
│   └── scheduler.py               # 4:00 PM IST daily job scheduler daemon & crontab generator
│
├── db/                            # Database connections, migrations & schemas
│   ├── __init__.py
│   ├── mongo.py                   # MongoDB client connection and batch write utilities
│   └── schema.sql                 # PostgreSQL DDL table schemas and indexes
│
├── server/                        # FastAPI REST API
│   ├── main.py                    # API endpoints (/refresh, /refresh/status)
│   ├── requirements.txt           # API specific dependencies
│   └── README.md                  # Server documentation
│
└── logs/                          # Runtime log output directory
```

---

## ⚙️ Configuration & Environment Variables (`config.py`)

All global parameters and directory paths are centrally defined in `config.py`:

| Variable | Default Value / Env Var | Description |
| :--- | :--- | :--- |
| `BASE_DIR` | `<project_root>` | Root directory of the repository |
| `DATA_DIR` | `<project_root>/data` | Directory for storing CSVs and JSON artifacts |
| `SYMBOLS_CSV` | `data/symbols.csv` | Active NSE equities CSV file path |
| `NIFTY_INDICES_JSON` | `data/nifty_indices.json` | Master list of NSE indices |
| `INDICES_DATA_JSON` | `data/indices_data.json` | RRG / index metrics JSON output |
| `BUY_CONFIRMED_JSON`| `data/buy_confirmed_watchlist.json` | Confirmed breakout output path |
| `BUY_SIGNAL_JSON` | `data/buy_signal_watchlist.json` | Buy signal watchlist output path |
| `DATABASE_URL` | `postgresql://postgres:password@localhost:5432/nse_scanner` | PostgreSQL connection URL |
| `MONGODB_URI` | `mongodb://localhost:27017` | MongoDB connection URI |
| `MONGODB_DB` | `nse_scanner` | MongoDB database name |
| `DOWNLOAD_PERIOD` | `"2y"` | Default Yahoo Finance history window (provides warmup for ATR(55)) |
| `BATCH_SIZE` | `50` | Number of symbols per Yahoo Finance batch download |
| `UT_BOT_ATR_PERIOD` | `55` | ATR look-back period for UT Bot |
| `UT_BOT_KEY_VALUE` | `1.0` | Sensitivity multiplier for ATR Trailing Stop |

---

## 🛠️ Unified CLI Reference (`main.py`)

Run commands from the repository root using `python main.py <command> [options]`.

### Ingestion Commands
- **`python main.py fetch-symbols`**  
  Downloads active NSE equities and populates the `stocks` table in PostgreSQL and saves `data/symbols.csv`.
- **`python main.py download`**  
  Downloads stock OHLCV history to PostgreSQL.  
  *Options*:  
  - `--test`: Download only the first symbol.  
  - `--symbol RELIANCE`: Download for a single symbol.  
  - `--recent`: Incremental mode (fetches only last 5 days).  
  - `--period 1mo`: Override download timeframe (e.g. `5d`, `1mo`, `6mo`, `2y`).
- **`python main.py download-indices`**  
  Downloads 5-year OHLCV history for all NSE indices from Upstox API.  
  *Options*: `--test` (first index only).
- **`python main.py fetch-indices-master`**  
  Downloads and parses the NSE Upstox instruments master into `data/nifty_indices.json`.

### Screening & Analysis Commands
- **`python main.py scan`**  
  Runs the UT Bot scanner across all active stocks for the given date.  
  *Options*:  
  - `--date YYYY-MM-DD`: Scan date (default: today).  
  - `--symbol RELIANCE`: Run scan on a specific symbol.  
  - `--days 1`: Number of past candles to scan.
- **`python main.py breakout`**  
  Evaluates active buy signals in `buy_watch_list` for breakout confirmation and writes to `confirmed_breakouts`.  
  *Options*:  
  - `--symbol RELIANCE`: Test a single symbol.  
  - `--dry-run`: Display results without database commits.
- **`python main.py analyze-indices`**  
  Refreshes index candles, computes Relative Rotation Graph (RRG) metrics (RS-Ratio, RS-Momentum, quadrant classification), and outputs `data/indices_data.json` & MongoDB.  
  *Options*:  
  - `--no-refresh`: Skip Upstox download and evaluate existing DB candles.  
  - `--benchmark NIFTY`: Benchmark index symbol (default: `NIFTY`).

### Export & Visualization Commands
- **`python main.py export`**  
  Exports `data/buy_confirmed_watchlist.json` & `data/buy_signal_watchlist.json`, syncs MongoDB, and launches TradingView charts in browser.  
  *Options*:  
  - `--json-only`: Export JSON files without opening browser tabs.  
  - `--batch-size 5`: Tabs to open per batch (default: 5).  
  - `--limit 10`: Max symbols to open.  
  - `--no-prompt`: Open all tabs without interactive prompts.

### Pipeline & Automation Commands
- **`python main.py run`**  
  Executes the full end-to-end stock pipeline in sequential order:  
  `download (--recent)` ➔ `scan` ➔ `breakout` ➔ `export`
- **`python main.py run-all`**  
  Executes the entire market refresh: `analyze-indices` ➔ `run`
- **`python main.py schedule`**  
  Runs the persistent 4:00 PM IST daily scheduler.  
  *Options*:  
  - `--cron`: Prints the standard crontab entry and exits.  
  - `--run-now`: Runs the pipeline immediately once.

---

## 🔄 Daily Workflow & Pipeline Architecture

```mermaid
flowchart TD
    A[4:00 PM IST Trigger\nScheduler / Cron / API] --> B[analyze-indices\nUpstox API ➔ RRG Analysis]
    B --> C[download --recent\nYahoo Finance ➔ daily_candles]
    C --> D[scan\nHeikin Ashi + UT Bot ➔ buy_watch_list]
    D --> E[breakout\nBreakout Check ➔ confirmed_breakouts]
    E --> F[export\nWrite JSONs ➔ Sync MongoDB ➔ Open TradingView]
```

---

## 🗄️ Database Architecture

### PostgreSQL Tables (`db/schema.sql`)
1. **`stocks`**: Master list of equities and index symbols (`symbol`, `company_name`, `exchange`, `is_active`).
2. **`daily_candles`**: OHLCV candle storage (`symbol`, `candle_date`, `open`, `high`, `low`, `close`, `volume`). Unique on `(symbol, candle_date)`.
3. **`scan_results`**: Historical log of every buy/sell signal emitted by scanner.
4. **`latest_buy_signal`**: Rolling latest BUY signal state per symbol.
5. **`buy_watch_list`**: Active BUY candidates waiting for breakout or invalidation.
6. **`confirmed_breakouts`**: Verified breakout candidates satisfying follow-through conditions.

### MongoDB Collections (`db/mongo.py`)
- **`buy_signal_data`**: JSON document store of all active buy signals.
- **`buy_confirmed_data`**: JSON document store of confirmed breakouts.
- **`indices_data`**: Sector and index RRG performance metrics.

---

## 🚀 Adding New Features

### 1. Adding a New Data Fetcher
- Create a new module in `fetchers/<name>.py`.
- Define output file paths in `config.py` using `DATA_DIR`.
- Register a CLI subcommand in `main.py`.

### 2. Adding a New Technical Indicator
- Create `indicators/<indicator_name>.py`.
- Ensure functions accept pandas DataFrames / Series and return structured indicator columns.
- Export in `indicators/__init__.py`.

### 3. Adding a New Screening Strategy
- Create `scanners/<strategy_name>.py`.
- Import indicators from `indicators` and database utilities from `config` / `db`.
- Register the strategy command in `main.py`.

### 4. Adding a New Exporter or Webhook Alert
- Create `exporters/<channel_name>.py` (e.g. Telegram, Discord, CSV/Excel).
- Hook into the pipeline inside `cmd_run` or `cmd_export` in `main.py`.
