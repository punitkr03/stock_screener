"""
analyze_indices.py
------------------
1.  Refreshes the last 7 days of candle data for every NSE index (same Upstox
    API as indices_download.py but with a 7-day window).
2.  Reads all stored candles from the daily_candles table (exchange='NSE_INDEX').
3.  Computes per-index metrics:
        • Week-on-week   (5 trading-day look-back)
        • Month-on-month (21 trading-day look-back)
        • Quarter        (63 trading-day look-back)
        • Half-year      (126 trading-day look-back)
        • Year           (252 trading-day look-back)
        • RS-Ratio       (JdK-style relative strength vs benchmark, normalised → 100)
        • RS-Momentum    (rate-of-change of RS-Ratio, normalised → 100)
        • quadrant       ("leading" | "weakening" | "improving" | "lagging")
4.  Writes indices_data.json ready for RRG chart rendering and list display.

Usage:
    python3 analyze_indices.py
    python3 analyze_indices.py --no-refresh          # skip 7-day download
    python3 analyze_indices.py --benchmark "NIFTY 50"
    python3 analyze_indices.py --output my_out.json
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, timedelta
from urllib.parse import quote

import pandas as pd
import requests
from sqlalchemy import create_engine, text

from config import DATABASE_URL

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTH_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ"
    ".eyJzdWIiOiI1WUNOR1IiLCJqdGkiOiI2YTUyNjU1NjIyNzQ0MzM3NWI4MjdjMDIi"
    "LCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6dHJ1ZSwiaXNFeHRlbm"
    "RlZCI6dHJ1ZSwiaWF0IjoxNzgzNzg0NzkwLCJpc3MiOiJ1ZGFwaS1nYXRld2F5LXNl"
    "cnZpY2UiLCJleHAiOjE4MTUzNDMyMDB9"
    ".ncmHhqx6Kbkw_kA21XI5I1Zj8-B7dhBoUDYNuiwo0Zo"
)

INDICES_JSON = "nifty_indices.json"
OUTPUT_JSON = "indices_data.json"

REFRESH_DAYS = 7       # calendar days to re-pull on each run
DELAY_SECS = 0.8       # polite delay between API calls

DEFAULT_BENCHMARK = "NIFTY"  # trading_symbol for Nifty 50 in nifty_indices.json / daily_candles

# Trading-day look-back windows for returns
LOOKBACKS: dict[str, int] = {
    "wow":   5,
    "mom":  21,
    "qoq":  63,
    "hoh": 126,
    "yoy": 252,
}

# EWM span for JdK RS-Ratio smoothing
RRG_SMOOTH = 10

# Bars to include in sparkline history
HISTORY_POINTS = 60

engine = create_engine(DATABASE_URL)


# ---------------------------------------------------------------------------
# Step 1 – Refresh last REFRESH_DAYS calendar days via Upstox API
# ---------------------------------------------------------------------------

def load_indices() -> list[dict]:
    """Load NSE index definitions from INDICES_JSON."""
    with open(INDICES_JSON, "r") as fh:
        return json.load(fh)


def ensure_index_in_stocks(symbol: str, name: str) -> None:
    """Upsert a row in the stocks master table for this index."""
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO stocks (symbol, company_name, exchange)
                VALUES (:symbol, :company_name, 'NSE_INDEX')
                ON CONFLICT (symbol)
                DO UPDATE SET
                    company_name = EXCLUDED.company_name,
                    updated_at   = NOW()
            """),
            {"symbol": symbol, "company_name": name},
        )


def refresh_recent_candles(indices: list[dict], days: int = REFRESH_DAYS) -> None:
    """Download and upsert the last `days` calendar days for every index."""
    to_date = date.today()
    from_date = to_date - timedelta(days=days)

    headers = {
        "Authorization": f"Bearer {AUTH_TOKEN}",
        "Accept": "application/json",
    }

    print(
        f"\n[Refresh] Fetching last {days} calendar days "
        f"({from_date} → {to_date}) for {len(indices)} indices …"
    )

    for i, idx in enumerate(indices, start=1):
        name = idx["name"]
        instrument_key = idx["instrument_key"]
        symbol = idx["trading_symbol"]

        encoded_key = quote(instrument_key, safe="")
        url = (
            f"https://api.upstox.com/v3/historical-candle/"
            f"{encoded_key}/days/1/"
            f"{to_date.isoformat()}/"
            f"{from_date.isoformat()}"
        )

        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code != 200:
                print(f"  [{i:3d}] {name}: API {r.status_code} – skipping")
                continue

            candles = r.json()["data"]["candles"]
            if not candles:
                print(f"  [{i:3d}] {name}: no candles returned")
                continue

            df = pd.DataFrame(
                candles,
                columns=["candle_date", "open", "high", "low", "close", "volume", "oi"],
            )
            df["symbol"] = symbol
            df["candle_date"] = pd.to_datetime(df["candle_date"]).dt.date
            df = df[["symbol", "candle_date", "open", "high", "low", "close", "volume"]]

            ensure_index_in_stocks(symbol, name)

            with engine.begin() as conn:
                for _, row in df.iterrows():
                    conn.exec_driver_sql(
                        """
                        INSERT INTO daily_candles
                        (symbol,candle_date,open,high,low,close,volume)
                        VALUES
                        (%(symbol)s,%(candle_date)s,%(open)s,%(high)s,
                         %(low)s,%(close)s,%(volume)s)
                        ON CONFLICT (symbol,candle_date)
                        DO UPDATE SET
                            open   = EXCLUDED.open,
                            high   = EXCLUDED.high,
                            low    = EXCLUDED.low,
                            close  = EXCLUDED.close,
                            volume = EXCLUDED.volume
                        """,
                        row.to_dict(),
                    )

            print(f"  [{i:3d}] {name}: {len(df)} candle(s) upserted")

        except Exception as exc:  # noqa: BLE001
            print(f"  [{i:3d}] {name}: Error – {exc}")

        if i != len(indices):
            time.sleep(DELAY_SECS)

    print("[Refresh] Done.\n")


# ---------------------------------------------------------------------------
# Step 2 – Load candles from DB
# ---------------------------------------------------------------------------

def load_all_candles() -> dict[str, pd.DataFrame]:
    """
    Load all NSE_INDEX candles from daily_candles, keyed by trading symbol.

    Returns:
        dict: symbol → DataFrame(index=DatetimeIndex asc,
                                  columns=[open, high, low, close, volume])
    """
    query = text("""
        SELECT dc.symbol, dc.candle_date,
               dc.open, dc.high, dc.low, dc.close, dc.volume
        FROM  daily_candles dc
        JOIN  stocks s ON s.symbol = dc.symbol
        WHERE s.exchange = 'NSE_INDEX'
        ORDER BY dc.symbol, dc.candle_date
    """)

    with engine.connect() as conn:
        df = pd.read_sql(query, conn, parse_dates=["candle_date"])

    result: dict[str, pd.DataFrame] = {}
    for symbol, grp in df.groupby("symbol"):
        grp = grp.set_index("candle_date").sort_index()
        result[str(symbol)] = grp[["open", "high", "low", "close", "volume"]]

    return result


# ---------------------------------------------------------------------------
# Step 3 – Metric computation helpers
# ---------------------------------------------------------------------------

def pct_change_at(series: pd.Series, lookback: int) -> float | None:
    """
    Percentage change from `lookback` bars ago to the latest bar.
    Returns None if insufficient history.
    """
    if len(series) <= lookback:
        return None
    current = float(series.iloc[-1])
    past = float(series.iloc[-(lookback + 1)])
    if past == 0:
        return None
    return round((current / past - 1) * 100, 4)


def compute_rs_ratio_and_momentum(
    index_close: pd.Series,
    bench_close: pd.Series,
    smooth: int = RRG_SMOOTH,
) -> tuple[float | None, float | None]:
    """
    JdK-style RS-Ratio and RS-Momentum, both normalised to 100.

    Algorithm:
      RS-raw      = (index_close / bench_close) * 100   (relative price)
      RS-Ratio    = EWM(RS-raw, span) → z-score scaled → mean=100, σ=10
      RS-Momentum = EWM(RS-Ratio, span) → z-score scaled → mean=100, σ=10

    >100 = outperforming / improving; <100 = underperforming / deteriorating.
    """
    aligned = pd.DataFrame({"idx": index_close, "bench": bench_close}).dropna()

    if len(aligned) < smooth + 10:
        return None, None

    rs_raw = (aligned["idx"] / aligned["bench"]) * 100

    # RS-Ratio ─────────────────────────────────────────────────────────────
    rr_ema = rs_raw.ewm(span=smooth, adjust=False).mean()
    rr_mean, rr_std = rr_ema.mean(), rr_ema.std()
    if rr_std == 0:
        return None, None
    rs_ratio_norm = 100 + ((rr_ema - rr_mean) / rr_std) * 10
    rs_ratio_val = round(float(rs_ratio_norm.iloc[-1]), 4)

    # RS-Momentum ──────────────────────────────────────────────────────────
    rm_ema = rs_ratio_norm.ewm(span=smooth, adjust=False).mean()
    rm_mean, rm_std = rm_ema.mean(), rm_ema.std()
    if rm_std == 0:
        return rs_ratio_val, None
    rs_mom_norm = 100 + ((rm_ema - rm_mean) / rm_std) * 10
    rs_mom_val = round(float(rs_mom_norm.iloc[-1]), 4)

    return rs_ratio_val, rs_mom_val


def quadrant(rs_ratio: float | None, rs_momentum: float | None) -> str:
    """Map (RS-Ratio, RS-Momentum) to one of the four RRG quadrant labels."""
    if rs_ratio is None or rs_momentum is None:
        return "unknown"
    if rs_ratio >= 100 and rs_momentum >= 100:
        return "leading"
    if rs_ratio >= 100 and rs_momentum < 100:
        return "weakening"
    if rs_ratio < 100 and rs_momentum >= 100:
        return "improving"
    return "lagging"


# ---------------------------------------------------------------------------
# Step 3 – Main metrics dispatcher
# ---------------------------------------------------------------------------

def compute_metrics(
    candles: dict[str, pd.DataFrame],
    benchmark: str,
    indices_meta: list[dict],
) -> list[dict]:
    """
    Iterate over every index in `candles`, compute all metrics, return a
    sorted list of dicts (sorted by quadrant priority then rs_ratio desc).
    """
    if benchmark not in candles:
        raise ValueError(
            f"Benchmark '{benchmark}' not found in DB. "
            "Run indices_download.py first to seed historical data."
        )

    bench_close = candles[benchmark]["close"]
    meta_map: dict[str, dict] = {m["trading_symbol"]: m for m in indices_meta}

    results: list[dict] = []

    for symbol, df in candles.items():
        close = df["close"]
        if len(close) == 0:
            continue

        latest_close = round(float(close.iloc[-1]), 4)
        latest_date = str(df.index[-1].date())

        # Per-period percentage returns
        returns: dict[str, float | None] = {
            period: pct_change_at(close, lb) for period, lb in LOOKBACKS.items()
        }

        # RRG  (benchmark compares against itself → tag it explicitly)
        if symbol == benchmark:
            rs_ratio, rs_momentum = 100.0, 100.0
            trend = "benchmark"
        else:
            rs_ratio, rs_momentum = compute_rs_ratio_and_momentum(close, bench_close)
            trend = quadrant(rs_ratio, rs_momentum)

        # Sparkline history
        hist_slice = close.iloc[-HISTORY_POINTS:]
        history_closes = [round(float(v), 4) for v in hist_slice.tolist()]
        history_dates = [str(d.date()) for d in hist_slice.index]

        meta = meta_map.get(symbol, {})

        results.append(
            {
                "symbol": symbol,
                "name": meta.get("name", symbol),
                "instrument_key": meta.get("instrument_key", ""),
                "latest_close": latest_close,
                "latest_date": latest_date,
                # ── Multi-period returns (%) ──────────────────────────────
                "returns": {
                    "wow": returns["wow"],   # week-on-week
                    "mom": returns["mom"],   # month-on-month
                    "qoq": returns["qoq"],   # quarter
                    "hoh": returns["hoh"],   # half-year
                    "yoy": returns["yoy"],   # year
                },
                # ── RRG metrics ───────────────────────────────────────────
                "rrg": {
                    "rs_ratio":    rs_ratio,
                    "rs_momentum": rs_momentum,
                    "quadrant":    trend,
                    "benchmark":   benchmark,
                },
                # ── Sparkline data ────────────────────────────────────────
                "history": {
                    "dates":  history_dates,
                    "closes": history_closes,
                },
            }
        )

    # Sort: benchmark first, then leading → improving → weakening → lagging → unknown
    # Within each quadrant: descending rs_ratio
    order = {"benchmark": 0, "leading": 1, "improving": 2, "weakening": 3, "lagging": 4, "unknown": 5}
    results.sort(
        key=lambda x: (
            order.get(x["rrg"]["quadrant"], 5),
            -(x["rrg"]["rs_ratio"] or 0.0),
        )
    )
    return results


# ---------------------------------------------------------------------------
# Step 4 – Build final JSON envelope
# ---------------------------------------------------------------------------

def build_output(
    results: list[dict],
    benchmark: str,
    generated_at: str,
) -> dict:
    """
    Wrap the result list in a top-level envelope.

    Schema compatible with:
      • RRG chart libraries (x=rs_ratio, y=rs_momentum, label=symbol)
      • Frontend list renderers (grouped by quadrant, sortable by any return)
    """
    summary: dict[str, list[str]] = {
        "benchmark": [],
        "leading":   [],
        "improving": [],
        "weakening": [],
        "lagging":   [],
        "unknown":   [],
    }
    for r in results:
        summary.setdefault(r["rrg"]["quadrant"], []).append(r["symbol"])

    return {
        "meta": {
            "generated_at":  generated_at,
            "benchmark":     benchmark,
            "total_indices": len(results),
            "periods": {
                "wow": "Week-on-Week (~5 trading days)",
                "mom": "Month-on-Month (~21 trading days)",
                "qoq": "Quarter (~63 trading days)",
                "hoh": "Half-Year (~126 trading days)",
                "yoy": "Year (~252 trading days)",
            },
            "rrg_notes": {
                "rs_ratio": (
                    "JdK RS-Ratio normalised to 100. "
                    ">100 = outperforming benchmark."
                ),
                "rs_momentum": (
                    "Rate-of-change of RS-Ratio, normalised to 100. "
                    ">100 = improving relative strength."
                ),
                "quadrant_definitions": {
                    "leading":   "rs_ratio >= 100 AND rs_momentum >= 100",
                    "weakening": "rs_ratio >= 100 AND rs_momentum <  100",
                    "improving": "rs_ratio <  100 AND rs_momentum >= 100",
                    "lagging":   "rs_ratio <  100 AND rs_momentum <  100",
                },
            },
        },
        "summary": summary,
        "indices": results,
    }


# ---------------------------------------------------------------------------
# Main entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyse NSE index candles and output RRG-compatible JSON."
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Skip the 7-day data-refresh step; use existing DB data only.",
    )
    parser.add_argument(
        "--benchmark",
        default=DEFAULT_BENCHMARK,
        help=f"Benchmark symbol for RRG (default: '{DEFAULT_BENCHMARK}' = Nifty 50).",
    )
    parser.add_argument(
        "--output",
        default=OUTPUT_JSON,
        help=f"Output JSON path (default: {OUTPUT_JSON}).",
    )
    args = parser.parse_args()

    indices = load_indices()

    # ── 1. Refresh ────────────────────────────────────────────────────────────
    if not args.no_refresh:
        refresh_recent_candles(indices, days=REFRESH_DAYS)
    else:
        print("[Refresh] Skipped (--no-refresh).")

    # ── 2. Load ───────────────────────────────────────────────────────────────
    print("[Analysis] Loading candle history from DB …")
    candles = load_all_candles()
    print(f"[Analysis] {len(candles)} index symbol(s) loaded.")

    if not candles:
        print("No candle data in DB. Run indices_download.py first.")
        return

    # ── 3. Compute ────────────────────────────────────────────────────────────
    print(f"[Analysis] Computing metrics (benchmark = {args.benchmark!r}) …")
    results = compute_metrics(candles, benchmark=args.benchmark, indices_meta=indices)

    # ── 4. Write ──────────────────────────────────────────────────────────────
    generated_at = pd.Timestamp.now(tz="Asia/Kolkata").isoformat()
    output = build_output(results, benchmark=args.benchmark, generated_at=generated_at)

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)

    s = output["summary"]
    print(f"\n[Done] {len(results)} indices → '{args.output}'")
    print(
        f"       Leading:   {len(s['leading'])}\n"
        f"       Improving: {len(s['improving'])}\n"
        f"       Weakening: {len(s['weakening'])}\n"
        f"       Lagging:   {len(s['lagging'])}"
    )


if __name__ == "__main__":
    main()
