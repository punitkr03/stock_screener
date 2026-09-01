"""
crude_oil/db.py

Database integration for Crude Oil Mini (CRUDEOILM).
Handles table creation, batch upsert of 5-minute candles, and status queries.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, List, Optional

import pandas as pd
from sqlalchemy import create_engine, text

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import DATABASE_URL

log = logging.getLogger(__name__)

_engine = None


def get_db_engine():
    """Return cached SQLAlchemy engine."""
    global _engine
    if _engine is None:
        _engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    return _engine


def init_db(engine=None) -> None:
    """Create crude_oil_data table and indexes if not exists."""
    eng = engine or get_db_engine()
    create_sql = """
    CREATE TABLE IF NOT EXISTS crude_oil_data (
        timestamp               TIMESTAMPTZ PRIMARY KEY,
        symbol                  TEXT NOT NULL DEFAULT 'CRUDEOILM',
        instrument_key          TEXT NOT NULL,
        open                    DOUBLE PRECISION NOT NULL,
        high                    DOUBLE PRECISION NOT NULL,
        low                     DOUBLE PRECISION NOT NULL,
        close                   DOUBLE PRECISION NOT NULL,
        volume                  BIGINT NOT NULL,
        open_interest           BIGINT NOT NULL DEFAULT 0,
        pcr                     DOUBLE PRECISION,
        ha_open                 DOUBLE PRECISION,
        ha_high                 DOUBLE PRECISION,
        ha_low                  DOUBLE PRECISION,
        ha_close                DOUBLE PRECISION,
        atr                     DOUBLE PRECISION,
        trailing_stop           DOUBLE PRECISION,
        signal                  TEXT NOT NULL DEFAULT 'NONE',
        buy_confirmed           BOOLEAN NOT NULL DEFAULT FALSE,
        created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_crude_oil_timestamp ON crude_oil_data(timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_crude_oil_signal ON crude_oil_data(signal);
    CREATE INDEX IF NOT EXISTS idx_crude_oil_confirmed ON crude_oil_data(buy_confirmed);
    """
    with eng.begin() as conn:
        conn.execute(text(create_sql))
    log.info("crude_oil_data table initialized.")


def save_candles_to_db(df: pd.DataFrame, engine=None) -> int:
    """
    Upsert DataFrame of processed 5-minute candles into crude_oil_data.
    Returns the count of rows stored.
    """
    if df.empty:
        return 0

    init_db(engine)
    eng = engine or get_db_engine()

    upsert_sql = """
    INSERT INTO crude_oil_data (
        timestamp, symbol, instrument_key, open, high, low, close, volume, open_interest,
        pcr, ha_open, ha_high, ha_low, ha_close, atr, trailing_stop, signal, buy_confirmed, updated_at
    ) VALUES (
        :timestamp, :symbol, :instrument_key, :open, :high, :low, :close, :volume, :open_interest,
        :pcr, :ha_open, :ha_high, :ha_low, :ha_close, :atr, :trailing_stop, :signal, :buy_confirmed, NOW()
    )
    ON CONFLICT (timestamp) DO UPDATE SET
        symbol         = EXCLUDED.symbol,
        instrument_key = EXCLUDED.instrument_key,
        open           = EXCLUDED.open,
        high           = EXCLUDED.high,
        low            = EXCLUDED.low,
        close          = EXCLUDED.close,
        volume         = EXCLUDED.volume,
        open_interest  = EXCLUDED.open_interest,
        pcr            = COALESCE(EXCLUDED.pcr, crude_oil_data.pcr),
        ha_open        = EXCLUDED.ha_open,
        ha_high        = EXCLUDED.ha_high,
        ha_low         = EXCLUDED.ha_low,
        ha_close       = EXCLUDED.ha_close,
        atr            = EXCLUDED.atr,
        trailing_stop  = EXCLUDED.trailing_stop,
        signal         = EXCLUDED.signal,
        buy_confirmed  = EXCLUDED.buy_confirmed,
        updated_at     = NOW();
    """

    records = []
    for _, row in df.iterrows():
        ts = row["timestamp"]
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()

        records.append({
            "timestamp": ts,
            "symbol": str(row.get("symbol", "CRUDEOILM")),
            "instrument_key": str(row.get("instrument_key", "")),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": int(row.get("volume", 0) or 0),
            "open_interest": int(row.get("open_interest", 0) or 0),
            "pcr": float(row["pcr"]) if pd.notnull(row.get("pcr")) else None,
            "ha_open": float(row["ha_open"]) if pd.notnull(row.get("ha_open")) else None,
            "ha_high": float(row["ha_high"]) if pd.notnull(row.get("ha_high")) else None,
            "ha_low": float(row["ha_low"]) if pd.notnull(row.get("ha_low")) else None,
            "ha_close": float(row["ha_close"]) if pd.notnull(row.get("ha_close")) else None,
            "atr": float(row["atr"]) if pd.notnull(row.get("atr")) else None,
            "trailing_stop": float(row["trailing_stop"]) if pd.notnull(row.get("trailing_stop")) else None,
            "signal": str(row.get("signal", "NONE")),
            "buy_confirmed": bool(row.get("buy_confirmed", False)),
        })

    with eng.begin() as conn:
        conn.execute(text(upsert_sql), records)

    log.info("Saved %s candle rows into crude_oil_data.", len(records))
    return len(records)


def load_candles_from_db(limit: Optional[int] = None, engine=None) -> pd.DataFrame:
    """Load stored candles from crude_oil_data table sorted chronologically."""
    eng = engine or get_db_engine()
    query = "SELECT * FROM crude_oil_data ORDER BY timestamp ASC"
    if limit:
        query = f"SELECT * FROM (SELECT * FROM crude_oil_data ORDER BY timestamp DESC LIMIT {int(limit)}) sub ORDER BY timestamp ASC"

    with eng.connect() as conn:
        df = pd.read_sql_query(text(query), conn)

    if not df.empty and "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])

    return df


def get_latest_signal_status(limit: int = 10, engine=None) -> Dict[str, Any]:
    """
    Query the last 10 candles along with PCR, start time, end time, and strategy flags.
    Used by FastAPI status endpoint and CLI.
    """
    from datetime import timedelta

    init_db(engine)
    eng = engine or get_db_engine()

    with eng.connect() as conn:
        # Total count
        count_res = conn.execute(text("SELECT COUNT(*) FROM crude_oil_data")).scalar() or 0

        # Last N candles
        last_n_rows = conn.execute(
            text(f"""
                SELECT * FROM (
                    SELECT * FROM crude_oil_data ORDER BY timestamp DESC LIMIT {int(limit)}
                ) sub ORDER BY timestamp ASC
            """)
        ).fetchall()

    from datetime import timedelta, timezone

    def format_candle(r):
        if not r:
            return None
        d = dict(r._mapping)
        ts = d.get("timestamp")
        if ts:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = ts.astimezone(timezone.utc)
        end_ts = ts + timedelta(minutes=5) if ts else None

        return {
            "candle_start_time": ts.isoformat() if ts else None,
            "candle_end_time": end_ts.isoformat() if end_ts else None,

            "open": d.get("open"),
            "high": d.get("high"),
            "low": d.get("low"),
            "close": d.get("close"),
            "volume": d.get("volume"),
            "open_interest": d.get("open_interest"),
            "pcr": d.get("pcr"),
            "ha_open": d.get("ha_open"),
            "ha_high": d.get("ha_high"),
            "ha_low": d.get("ha_low"),
            "ha_close": d.get("ha_close"),
            "atr": d.get("atr"),
            "trailing_stop": d.get("trailing_stop"),
            "signal": d.get("signal", "NONE"),
            "buy_confirmed": d.get("buy_confirmed", False),
        }


    candles = [format_candle(r) for r in last_n_rows]
    latest_candle = candles[-1] if candles else None

    # Resolve latest overall status
    current_signal = latest_candle.get("signal", "NONE") if latest_candle else "NONE"
    buy_confirmed = latest_candle.get("buy_confirmed", False) if latest_candle else False
    current_pcr = latest_candle.get("pcr") if latest_candle else None
    current_oi = latest_candle.get("open_interest") if latest_candle else 0

    return {
        "symbol": "CRUDEOILM",
        "total_candles": count_res,
        "current_signal": current_signal,
        "buy_confirmed": buy_confirmed,
        "pcr": current_pcr,
        "open_interest": current_oi,
        "latest_candle": latest_candle,
        "candles": candles,
    }

