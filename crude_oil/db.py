"""
crude_oil/db.py

Database integration for Crude Oil Mini (CRUDEOILM).
Handles table creation, batch upsert of 5-minute candles, and status queries.
"""

from datetime import datetime, timedelta, timezone
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
_db_initialized = False   # guard — init_db() DDL runs only once per process


def get_db_engine():
    """Return cached SQLAlchemy engine."""
    global _engine
    if _engine is None:
        _engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    return _engine


def init_db(engine=None) -> None:
    """
    Create all required tables and indexes on first call; subsequent calls are instant no-ops.

    The _db_initialized guard prevents the CREATE TABLE IF NOT EXISTS DDL from being
    re-sent to PostgreSQL on every function call. Without this guard, init_db fires
    5-6 times per daemon tick because it is called at the top of every helper function.

    Pass an explicit `engine` only when targeting a test database — doing so bypasses
    the guard and always runs the DDL.
    """
    global _db_initialized
    eng = engine or get_db_engine()

    # Skip DDL if already ran in this process (unless a custom engine is supplied)
    if _db_initialized and engine is None:
        return

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
        sell_confirmed          BOOLEAN NOT NULL DEFAULT FALSE,
        created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    ALTER TABLE crude_oil_data ADD COLUMN IF NOT EXISTS sell_confirmed BOOLEAN DEFAULT FALSE;

    CREATE INDEX IF NOT EXISTS idx_crude_oil_timestamp ON crude_oil_data(timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_crude_oil_signal ON crude_oil_data(signal);
    CREATE INDEX IF NOT EXISTS idx_crude_oil_confirmed ON crude_oil_data(buy_confirmed);
    CREATE INDEX IF NOT EXISTS idx_crude_oil_sell_confirmed ON crude_oil_data(sell_confirmed);

    CREATE TABLE IF NOT EXISTS crude_oil_pcr_data (
        timestamp               TIMESTAMPTZ PRIMARY KEY,
        symbol                  TEXT NOT NULL DEFAULT 'CRUDEOILM',
        instrument_key          TEXT NOT NULL,
        pcr                     DOUBLE PRECISION NOT NULL,
        pe_oi                   DOUBLE PRECISION NOT NULL DEFAULT 0,
        ce_oi                   DOUBLE PRECISION NOT NULL DEFAULT 0,
        created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_crude_oil_pcr_timestamp ON crude_oil_pcr_data(timestamp DESC);

    CREATE TABLE IF NOT EXISTS fcm_tokens (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        token       TEXT NOT NULL UNIQUE,
        label       TEXT,
        is_active   BOOLEAN NOT NULL DEFAULT TRUE,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_fcm_tokens_active ON fcm_tokens(is_active);

    CREATE TABLE IF NOT EXISTS crude_oil_signal_state (
        id              INT PRIMARY KEY DEFAULT 1,
        signal          TEXT NOT NULL DEFAULT 'NONE',
        pcr             DOUBLE PRECISION,
        avg_pcr_3       DOUBLE PRECISION,
        pcr_delta_pct   DOUBLE PRECISION,
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """
    with eng.begin() as conn:
        conn.execute(text(create_sql))
    _db_initialized = True
    log.info("crude_oil DB tables initialized (fcm_tokens, crude_oil_signal_state, crude_oil_data, crude_oil_pcr_data).")


def save_candles_to_db(df: pd.DataFrame, engine=None) -> int:
    """
    Upsert DataFrame of processed 5-minute candles into crude_oil_data.
    Returns the count of rows stored.
    """
    if df.empty:
        return 0

    eng = engine or get_db_engine()

    upsert_sql = """
    INSERT INTO crude_oil_data (
        timestamp, symbol, instrument_key, open, high, low, close, volume, open_interest,
        pcr, ha_open, ha_high, ha_low, ha_close, atr, trailing_stop, signal, buy_confirmed, sell_confirmed, updated_at
    ) VALUES (
        :timestamp, :symbol, :instrument_key, :open, :high, :low, :close, :volume, :open_interest,
        :pcr, :ha_open, :ha_high, :ha_low, :ha_close, :atr, :trailing_stop, :signal, :buy_confirmed, :sell_confirmed, NOW()
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
        sell_confirmed = EXCLUDED.sell_confirmed,
        updated_at     = NOW();
    """

    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp", ascending=True).reset_index(drop=True)


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
            "sell_confirmed": bool(row.get("sell_confirmed", False)),
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
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    return df


def get_latest_candle_timestamp(engine=None) -> Optional[datetime]:
    """Return the timestamp of the latest candle stored in crude_oil_data table (UTC)."""
    eng = engine or get_db_engine()
    with eng.connect() as conn:
        res = conn.execute(text("SELECT MAX(timestamp) FROM crude_oil_data")).scalar()
        if res is not None:
            if res.tzinfo is None:
                res = res.replace(tzinfo=timezone.utc)
            else:
                res = res.astimezone(timezone.utc)
        return res



def save_pcr_record_to_db(record: Dict[str, Any], engine=None) -> bool:
    """
    Save or upsert a single Put-Call Ratio (PCR) record into crude_oil_pcr_data table.
    Record structure:
        {
            "timestamp": datetime or ISO string,
            "symbol": str,
            "instrument_key": str,
            "pcr": float,
            "pe_oi": float,
            "ce_oi": float,
        }
    """
    if not record or record.get("pcr") is None:
        return False

    eng = engine or get_db_engine()

    ts = record.get("timestamp")
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    elif ts is None:
        ts = datetime.now(timezone.utc)
    elif hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)

    upsert_sql = """
    INSERT INTO crude_oil_pcr_data (
        timestamp, symbol, instrument_key, pcr, pe_oi, ce_oi, created_at
    ) VALUES (
        :timestamp, :symbol, :instrument_key, :pcr, :pe_oi, :ce_oi, NOW()
    )
    ON CONFLICT (timestamp) DO UPDATE SET
        symbol         = EXCLUDED.symbol,
        instrument_key = EXCLUDED.instrument_key,
        pcr            = EXCLUDED.pcr,
        pe_oi          = EXCLUDED.pe_oi,
        ce_oi          = EXCLUDED.ce_oi;
    """

    params = {
        "timestamp": ts,
        "symbol": str(record.get("symbol", "CRUDEOILM")),
        "instrument_key": str(record.get("instrument_key", "")),
        "pcr": float(record["pcr"]),
        "pe_oi": float(record.get("pe_oi", 0) or 0),
        "ce_oi": float(record.get("ce_oi", 0) or 0),
    }

    with eng.begin() as conn:
        conn.execute(text(upsert_sql), params)

    log.info("Saved PCR record at %s (PCR: %s, PE OI: %s, CE OI: %s) to DB.", ts.isoformat(), record["pcr"], record.get("pe_oi"), record.get("ce_oi"))
    return True


def load_pcr_history_from_db(limit: int = 50, engine=None) -> List[Dict[str, Any]]:
    """
    Load recent PCR history records sorted descending (latest first).
    """
    eng = engine or get_db_engine()

    query = f"""
    SELECT timestamp, symbol, instrument_key, pcr, pe_oi, ce_oi
    FROM crude_oil_pcr_data
    ORDER BY timestamp DESC
    LIMIT {int(limit)}
    """

    with eng.connect() as conn:
        rows = conn.execute(text(query)).fetchall()

    results = []
    for r in rows:
        d = dict(r._mapping)
        ts = d.get("timestamp")
        if ts:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = ts.astimezone(timezone.utc)
        results.append({
            "timestamp": ts.isoformat() if ts else None,
            "symbol": d.get("symbol"),
            "instrument_key": d.get("instrument_key"),
            "pcr": d.get("pcr"),
            "pe_oi": d.get("pe_oi"),
            "ce_oi": d.get("ce_oi"),
        })
    return results


def get_latest_signal_status(limit: int = 10, pcr_limit: int = 50, engine=None) -> Dict[str, Any]:
    """
    Query the last N candles along with PCR history, start time, end time, and strategy flags.
    Ordered descending: newest/latest candle first (candles[0] is latest).
    Used by FastAPI status endpoint and CLI.
    """
    from datetime import timedelta, timezone

    eng = engine or get_db_engine()

    with eng.connect() as conn:
        # Total count
        count_res = conn.execute(text("SELECT COUNT(*) FROM crude_oil_data")).scalar() or 0

        # Last N candles (ordered descending: newest/latest candle first)
        last_n_rows = conn.execute(
            text(f"""
                SELECT * FROM crude_oil_data ORDER BY timestamp DESC LIMIT {int(limit)}
            """)
        ).fetchall()

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
            "buy_confirmed": bool(d.get("buy_confirmed", False)),
            "sell_confirmed": bool(d.get("sell_confirmed", False)),
        }

    candles = [format_candle(r) for r in last_n_rows]
    latest_candle = candles[0] if candles else None

    # Load recent PCR history from dedicated crude_oil_pcr_data table
    pcr_history = load_pcr_history_from_db(limit=pcr_limit, engine=eng)

    # Resolve active contract metadata
    from crude_oil.fetcher import get_active_crude_mini_contract
    active_contract = get_active_crude_mini_contract()

    expiry_ms = active_contract.get("expiry")
    expiry_str = None
    if expiry_ms:
        try:
            expiry_str = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            expiry_str = str(expiry_ms)

    contract_info = {
        "trading_symbol": active_contract.get("trading_symbol", "CRUDEOILM FUT"),
        "instrument_key": active_contract.get("instrument_key", ""),
        "expiry_date": expiry_str,
        "exchange": active_contract.get("exchange", "MCX"),
        "segment": active_contract.get("segment", "MCX_FO"),
        "lot_size": active_contract.get("lot_size", 10),
        "price_quote_unit": active_contract.get("price_quote_unit", "BBL"),
        "underlying_symbol": active_contract.get("underlying_symbol", "CRUDEOILM"),
        "underlying_key": active_contract.get("underlying_key", ""),
    }

    # Resolve latest overall status
    current_signal = latest_candle.get("signal", "NONE") if latest_candle else "NONE"
    buy_confirmed = latest_candle.get("buy_confirmed", False) if latest_candle else False
    sell_confirmed = latest_candle.get("sell_confirmed", False) if latest_candle else False
    current_pcr = pcr_history[0]["pcr"] if pcr_history else (latest_candle.get("pcr") if latest_candle else None)
    current_oi = latest_candle.get("open_interest") if latest_candle else 0

    return {
        "symbol": "CRUDEOILM",
        "contract": contract_info,
        "total_candles": count_res,
        "current_signal": current_signal,
        "buy_confirmed": buy_confirmed,
        "sell_confirmed": sell_confirmed,
        "pcr": current_pcr,
        "open_interest": current_oi,
        "latest_candle": latest_candle,
        "candles": candles,
        "pcr_data": pcr_history,
    }


# ============================================================================
# FCM Token management
# ============================================================================

def register_fcm_token(token: str, label: Optional[str] = None, engine=None) -> bool:
    """
    Upsert an FCM device token into the fcm_tokens table.

    If the token already exists and was previously deregistered (is_active=FALSE),
    it is reactivated.  The label is updated only when supplied.

    Returns
    -------
    True  : token was newly inserted
    False : token already existed (may have been reactivated)
    """
    eng = engine or get_db_engine()
    sql = """
    INSERT INTO fcm_tokens (token, label, is_active, created_at, updated_at)
    VALUES (:token, :label, TRUE, NOW(), NOW())
    ON CONFLICT (token) DO UPDATE
        SET is_active  = TRUE,
            label      = COALESCE(EXCLUDED.label, fcm_tokens.label),
            updated_at = NOW()
    RETURNING (xmax = 0) AS inserted
    """
    with eng.begin() as conn:
        row = conn.execute(text(sql), {"token": token, "label": label}).fetchone()
    return bool(row[0]) if row else False


def deregister_fcm_token(token: str, engine=None) -> bool:
    """
    Soft-delete an FCM token by setting is_active=FALSE.

    Returns
    -------
    True  : token found and deactivated
    False : token not found
    """
    eng = engine or get_db_engine()
    sql = """
    UPDATE fcm_tokens
       SET is_active = FALSE,
           updated_at = NOW()
     WHERE token = :token
    RETURNING id
    """
    with eng.begin() as conn:
        row = conn.execute(text(sql), {"token": token}).fetchone()
    return row is not None


def get_active_fcm_tokens(engine=None) -> List[str]:
    """Return all active FCM token strings ordered by registration time."""
    eng = engine or get_db_engine()
    sql = "SELECT token FROM fcm_tokens WHERE is_active = TRUE ORDER BY created_at"
    with eng.connect() as conn:
        rows = conn.execute(text(sql)).fetchall()
    return [r[0] for r in rows]


# ============================================================================
# Signal state management
# ============================================================================

def get_signal_state(engine=None) -> Dict[str, Any]:
    """
    Return the persisted PCR signal state row (id=1), or an empty dict if
    no state has been saved yet.
    """
    eng = engine or get_db_engine()
    sql = """
    SELECT signal, pcr, avg_pcr_3, pcr_delta_pct, updated_at
      FROM crude_oil_signal_state
     WHERE id = 1
    """
    with eng.connect() as conn:
        row = conn.execute(text(sql)).fetchone()
    if not row:
        return {}
    d = dict(row._mapping)
    ts = d.get("updated_at")
    if ts is not None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
    return {
        "signal":        d.get("signal", "NONE"),
        "pcr":           d.get("pcr"),
        "avg_pcr_3":     d.get("avg_pcr_3"),
        "pcr_delta_pct": d.get("pcr_delta_pct"),
        "updated_at":    ts.isoformat() if ts else None,
    }


def save_signal_state(
    signal: str,
    pcr: Optional[float],
    avg_pcr_3: Optional[float],
    pcr_delta_pct: Optional[float],
    engine=None,
) -> None:
    """
    Upsert the single-row signal state record (id always = 1).
    Called on every PCR update regardless of whether the signal changed.
    """
    eng = engine or get_db_engine()
    sql = """
    INSERT INTO crude_oil_signal_state
        (id, signal, pcr, avg_pcr_3, pcr_delta_pct, updated_at)
    VALUES
        (1, :signal, :pcr, :avg_pcr_3, :pcr_delta_pct, NOW())
    ON CONFLICT (id) DO UPDATE
        SET signal        = EXCLUDED.signal,
            pcr           = EXCLUDED.pcr,
            avg_pcr_3     = EXCLUDED.avg_pcr_3,
            pcr_delta_pct = EXCLUDED.pcr_delta_pct,
            updated_at    = NOW()
    """
    with eng.begin() as conn:
        conn.execute(text(sql), {
            "signal":        signal,
            "pcr":           pcr,
            "avg_pcr_3":     avg_pcr_3,
            "pcr_delta_pct": pcr_delta_pct,
        })
    log.debug("Signal state saved: %s (PCR: %s, Avg-3: %s, Delta%%: %s)", signal, pcr, avg_pcr_3, pcr_delta_pct)
