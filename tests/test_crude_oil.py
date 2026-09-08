"""
tests/test_crude_oil.py

Unit and integration tests for Crude Oil Mini strategy engine,
PCR calculation, database operations, and FastAPI endpoints.
Compatible with standard python3 -m unittest.
"""

from datetime import datetime, timedelta
import unittest
import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo
from crude_oil.strategy import process_crude_oil_strategy
from crude_oil.fetcher import is_crude_oil_market_open
from crude_oil.db import (
    init_db,
    save_candles_to_db,
    load_candles_from_db,
    save_pcr_record_to_db,
    load_pcr_history_from_db,
    get_latest_candle_timestamp,
    get_latest_signal_status,
)
from server.main import app, get_crude_status_endpoint, root


def generate_sample_5m_candles(count: int = 100) -> pd.DataFrame:
    """Generate synthetic 5-minute OHLCV + OI candle data."""
    base_time = datetime(2026, 9, 1, 9, 15)
    rows = []
    price = 8000.0

    for i in range(count):
        t = base_time + timedelta(minutes=5 * i)
        if i < 40:
            price += np.random.uniform(-5, 15)
        elif i < 70:
            price += np.random.uniform(-15, 5)
        else:
            price += np.random.uniform(5, 20)

        o = price
        h = o + abs(np.random.uniform(2, 10))
        l = o - abs(np.random.uniform(2, 10))
        c = (o + h + l) / 3.0
        v = int(np.random.uniform(100, 2000))
        oi = int(np.random.uniform(20000, 40000))

        rows.append({
            "timestamp": t,
            "symbol": "CRUDEOILM",
            "instrument_key": "MCX_FO|565900",
            "open": round(o, 2),
            "high": round(h, 2),
            "low": round(l, 2),
            "close": round(c, 2),
            "volume": v,
            "open_interest": oi,
        })

    return pd.DataFrame(rows)


class TestCrudeOilStrategy(unittest.TestCase):
    def setUp(self):
        self.sample_df = generate_sample_5m_candles(100)

    def test_strategy_processing(self):
        """Test Heikin Ashi, UT Bot signals, Breakout, and PCR calculation on 5m candles."""
        df_out = process_crude_oil_strategy(
            self.sample_df,
            current_pcr=1.45,
            atr_period=10,
            key_value=1.0,
        )

        self.assertFalse(df_out.empty)
        self.assertEqual(len(df_out), len(self.sample_df))

        expected_cols = [
            "timestamp", "symbol", "instrument_key",
            "open", "high", "low", "close", "volume", "open_interest",
            "ha_open", "ha_high", "ha_low", "ha_close",
            "atr", "trailing_stop", "signal", "buy_confirmed", "sell_confirmed", "pcr"
        ]
        for col in expected_cols:
            self.assertIn(col, df_out.columns, f"Missing column {col}")

        # Verify PCR attached to latest candle
        self.assertEqual(df_out["pcr"].iloc[-1], 1.45)

        # Verify signals are valid enum
        valid_signals = {"BUY", "SELL", "NONE"}
        self.assertTrue(set(df_out["signal"].unique()).issubset(valid_signals))

        # Verify buy_confirmed and sell_confirmed are boolean
        self.assertTrue(set(df_out["buy_confirmed"].unique()).issubset({True, False}))
        self.assertTrue(set(df_out["sell_confirmed"].unique()).issubset({True, False}))

    def test_breakout_logic_progression(self):
        """Test that buy_confirmed and sell_confirmed flags exist and evaluate chronologically."""
        base_time = datetime(2026, 9, 1, 9, 0)
        rows = [
            {
                "timestamp": base_time + timedelta(minutes=5 * i),
                "symbol": "CRUDEOILM",
                "instrument_key": "TEST",
                "open": 8000.0 + i * 10,
                "high": 8010.0 + i * 10,
                "low": 7990.0 + i * 10,
                "close": 8005.0 + i * 10,
                "volume": 100,
                "open_interest": 5000,
            }
            for i in range(25)
        ]
        df = pd.DataFrame(rows)
        df_out = process_crude_oil_strategy(df, atr_period=5, key_value=1.0)
        self.assertIn("buy_confirmed", df_out.columns)
        self.assertIn("sell_confirmed", df_out.columns)

    def test_pcr_db_operations(self):
        """Test saving and loading dedicated 3-minute PCR history records."""
        init_db()
        t1 = datetime(2026, 9, 1, 10, 0)
        t2 = datetime(2026, 9, 1, 10, 3)

        rec1 = {
            "timestamp": t1,
            "symbol": "CRUDEOILM",
            "instrument_key": "MCX_FO|565900",
            "pcr": 1.25,
            "pe_oi": 125000.0,
            "ce_oi": 100000.0,
        }
        rec2 = {
            "timestamp": t2,
            "symbol": "CRUDEOILM",
            "instrument_key": "MCX_FO|565900",
            "pcr": 1.35,
            "pe_oi": 135000.0,
            "ce_oi": 100000.0,
        }

        self.assertTrue(save_pcr_record_to_db(rec1))
        self.assertTrue(save_pcr_record_to_db(rec2))

        history = load_pcr_history_from_db(limit=10)
        self.assertGreaterEqual(len(history), 2)
        # Verify descending order (latest first)
        self.assertGreaterEqual(history[0]["timestamp"], history[1]["timestamp"])

    def test_db_operations(self):
        """Test saving processed candles to PostgreSQL and querying status."""
        init_db()
        df_proc = process_crude_oil_strategy(self.sample_df, current_pcr=1.25, atr_period=10)
        saved_count = save_candles_to_db(df_proc)
        self.assertEqual(saved_count, len(self.sample_df))

        # Load back
        loaded = load_candles_from_db(limit=50)
        self.assertFalse(loaded.empty)
        self.assertEqual(len(loaded), 50)

        # Get status
        status = get_latest_signal_status()
        self.assertEqual(status["symbol"], "CRUDEOILM")
        self.assertIn("contract", status)
        self.assertIn("trading_symbol", status["contract"])
        self.assertIn("expiry_date", status["contract"])
        self.assertGreaterEqual(status["total_candles"], len(self.sample_df))
        self.assertIn("current_signal", status)
        self.assertIn("buy_confirmed", status)
        self.assertIn("sell_confirmed", status)
        self.assertIn("pcr", status)
        self.assertIn("pcr_data", status)
        self.assertIsInstance(status["pcr_data"], list)
        self.assertIn("latest_candle", status)

    def test_fastapi_endpoints(self):
        """Test FastAPI endpoint handlers directly."""
        # 1. Root health
        res_root = root()
        self.assertEqual(res_root["status"], "ok")

        # 2. Crude Oil Status endpoint
        status_data = get_crude_status_endpoint()
        self.assertEqual(status_data["symbol"], "CRUDEOILM")
        self.assertIn("contract", status_data)
        self.assertIn("buy_confirmed", status_data)
        self.assertIn("sell_confirmed", status_data)
        self.assertIn("pcr", status_data)
        self.assertIn("pcr_data", status_data)
        self.assertIsInstance(status_data["pcr_data"], list)
        self.assertIn("open_interest", status_data)


    def test_market_hours_logic(self):
        """Test MCX market hours evaluation (09:00 - 23:30 IST, Mon-Fri)."""
        ist = ZoneInfo("Asia/Kolkata")
        # Tuesday 10:00 AM IST -> Open
        t_open = datetime(2026, 9, 8, 10, 0, tzinfo=ist)
        self.assertTrue(is_crude_oil_market_open(t_open))

        # Tuesday 23:29 IST -> Open
        t_late = datetime(2026, 9, 8, 23, 29, tzinfo=ist)
        self.assertTrue(is_crude_oil_market_open(t_late))

        # Tuesday 23:36 IST -> Closed (past 5 min grace)
        t_after_close = datetime(2026, 9, 8, 23, 36, tzinfo=ist)
        self.assertFalse(is_crude_oil_market_open(t_after_close))

        # Tuesday 08:55 AM IST -> Closed (pre-market)
        t_early = datetime(2026, 9, 8, 8, 55, tzinfo=ist)
        self.assertFalse(is_crude_oil_market_open(t_early))

        # Sunday 12:00 PM IST -> Closed (weekend)
        t_weekend = datetime(2026, 9, 6, 12, 0, tzinfo=ist)
        self.assertFalse(is_crude_oil_market_open(t_weekend))

    def test_latest_candle_timestamp(self):
        """Test get_latest_candle_timestamp helper."""
        from sqlalchemy import text
        from crude_oil.db import get_db_engine
        init_db()
        t_fut = datetime(2099, 1, 1, 12, 0, tzinfo=ZoneInfo("UTC"))
        fut_df = pd.DataFrame([{
            "timestamp": t_fut,
            "symbol": "CRUDEOILM",
            "instrument_key": "TEST",
            "open": 8000.0,
            "high": 8010.0,
            "low": 7990.0,
            "close": 8005.0,
            "volume": 100,
            "open_interest": 5000,
        }])
        df_proc = process_crude_oil_strategy(fut_df, current_pcr=1.25)
        save_candles_to_db(df_proc)
        latest_ts = get_latest_candle_timestamp()
        self.assertEqual(latest_ts, t_fut)

        # Clean up test row
        with get_db_engine().begin() as conn:
            conn.execute(text("DELETE FROM crude_oil_data WHERE timestamp >= '2099-01-01'"))

    def test_pcr_throttling_and_market_hours(self):
        """Test 2-minute PCR throttling and market hours enforcement in update_crude_oil_pcr."""
        from crude_oil import update_crude_oil_pcr
        from crude_oil.db import get_db_engine
        from sqlalchemy import text
        from unittest.mock import patch

        init_db()
        eng = get_db_engine()

        # Clean up any previous test PCR rows for TEST_KEY
        with eng.begin() as conn:
            conn.execute(text("DELETE FROM crude_oil_pcr_data WHERE instrument_key = 'MCX_FO|TEST_KEY'"))

        now_utc = datetime.now(ZoneInfo("UTC"))

        # Save an initial PCR record
        rec = {
            "timestamp": now_utc,
            "symbol": "CRUDEOILM",
            "instrument_key": "MCX_FO|TEST_KEY",
            "pcr": 1.75,
            "pe_oi": 175000.0,
            "ce_oi": 100000.0,
        }
        save_pcr_record_to_db(rec)

        try:
            # 1. Throttling test: calling update_crude_oil_pcr within 120s should return cached record without calling API
            with patch("crude_oil.calculate_pcr_details") as mock_calc:
                cached = update_crude_oil_pcr(force=False, min_interval_seconds=120, ignore_market_hours=True)
                self.assertIsNotNone(cached)
                self.assertEqual(cached["pcr"], 1.75)
                mock_calc.assert_not_called()

            # 2. Market closed test: when market is closed and ignore_market_hours=False, calculation is skipped
            with patch("crude_oil.is_crude_oil_market_open", return_value=False), patch("crude_oil.calculate_pcr_details") as mock_calc:
                cached_mc = update_crude_oil_pcr(force=False, ignore_market_hours=False)
                self.assertIsNotNone(cached_mc)
                self.assertEqual(cached_mc["pcr"], 1.75)
                mock_calc.assert_not_called()

            # 3. Force override test: force=True triggers calculation even within 120s window
            with patch("crude_oil.calculate_pcr_details", return_value={
                "timestamp": now_utc + timedelta(seconds=1),
                "symbol": "CRUDEOILM",
                "instrument_key": "MCX_FO|TEST_KEY",
                "pcr": 1.80,
                "pe_oi": 180000.0,
                "ce_oi": 100000.0,
            }) as mock_calc:
                forced = update_crude_oil_pcr(force=True, ignore_market_hours=True)
                self.assertIsNotNone(forced)
                self.assertEqual(forced["pcr"], 1.80)
                mock_calc.assert_called_once()
        finally:
            with eng.begin() as conn:
                conn.execute(text("DELETE FROM crude_oil_pcr_data WHERE instrument_key = 'MCX_FO|TEST_KEY'"))



if __name__ == "__main__":
    unittest.main()

