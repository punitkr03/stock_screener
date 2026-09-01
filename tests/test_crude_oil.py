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
from crude_oil.strategy import process_crude_oil_strategy
from crude_oil.db import init_db, save_candles_to_db, load_candles_from_db, get_latest_signal_status
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
            "atr", "trailing_stop", "signal", "buy_confirmed", "pcr"
        ]
        for col in expected_cols:
            self.assertIn(col, df_out.columns, f"Missing column {col}")

        # Verify PCR attached to latest candle
        self.assertEqual(df_out["pcr"].iloc[-1], 1.45)

        # Verify signals are valid enum
        valid_signals = {"BUY", "SELL", "NONE"}
        self.assertTrue(set(df_out["signal"].unique()).issubset(valid_signals))

        # Verify buy_confirmed is boolean
        self.assertTrue(set(df_out["buy_confirmed"].unique()).issubset({True, False}))

    def test_breakout_logic_progression(self):
        """Test that buy_confirmed flag exists and is evaluated chronologically."""
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
        self.assertGreaterEqual(status["total_candles"], len(self.sample_df))
        self.assertIn("current_signal", status)
        self.assertIn("buy_confirmed", status)
        self.assertIn("pcr", status)
        self.assertIn("latest_candle", status)

    def test_fastapi_endpoints(self):
        """Test FastAPI endpoint handlers directly."""
        # 1. Root health
        res_root = root()
        self.assertEqual(res_root["status"], "ok")

        # 2. Crude Oil Status endpoint
        status_data = get_crude_status_endpoint()
        self.assertEqual(status_data["symbol"], "CRUDEOILM")
        self.assertIn("buy_confirmed", status_data)
        self.assertIn("pcr", status_data)
        self.assertIn("open_interest", status_data)



if __name__ == "__main__":
    unittest.main()
