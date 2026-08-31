"""
test_analytics.py
-----------------
Unit tests for analytics package:
- ISIN resolver
- Upstox key-ratios parser
- Stock metrics calculation (EPS * PE fair price, PS, Interest coverage, Sector comparisons)
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from analytics.isin_resolver import get_isin_for_symbol, _ISIN_CACHE
from analytics.engine import _parse_upstox_ratios, calculate_stock_metrics, _to_float


class TestAnalytics(unittest.TestCase):

    def test_to_float(self):
        self.assertEqual(_to_float(12.34), 12.34)
        self.assertEqual(_to_float("25.50"), 25.50)
        self.assertEqual(_to_float("1,234.56"), 1234.56)
        self.assertEqual(_to_float("15.2%"), 15.2)
        self.assertIsNone(_to_float("-"))
        self.assertIsNone(_to_float("N/A"))
        self.assertIsNone(_to_float(None))

    def test_parse_upstox_ratios_dict(self):
        sample_dict = {
            "pe": 25.4,
            "sector_pe": 20.0,
            "pb": 3.2,
            "sector_pb": 2.5,
            "price_to_sales": 2.1,
            "eps": 105.0,
        }
        res = _parse_upstox_ratios(sample_dict)
        self.assertEqual(res["pe"], 25.4)
        self.assertEqual(res["sector_pe"], 20.0)
        self.assertEqual(res["pb"], 3.2)
        self.assertEqual(res["sector_pb"], 2.5)
        self.assertEqual(res["ps"], 2.1)
        self.assertEqual(res["eps"], 105.0)

    def test_parse_upstox_ratios_list(self):
        sample_list = [
            {"indicator": "Price to Earnings (P/E)", "value": "28.5", "sector_average": "22.0"},
            {"indicator": "Price to Book (P/B)", "value": "2.4", "sector_average": "2.1"},
            {"indicator": "Price to Sales (P/S)", "value": "1.85", "sector_average": "1.50"},
            {"indicator": "EPS", "value": "85.2"},
        ]
        res = _parse_upstox_ratios(sample_list)
        self.assertEqual(res["pe"], 28.5)
        self.assertEqual(res["sector_pe"], 22.0)
        self.assertEqual(res["pb"], 2.4)
        self.assertEqual(res["sector_pb"], 2.1)
        self.assertEqual(res["ps"], 1.85)
        self.assertEqual(res["eps"], 85.2)

    @patch("analytics.engine.get_isin_for_symbol")
    @patch("analytics.engine.fetch_upstox_key_ratios")
    @patch("analytics.engine.fetch_yfinance_data")
    def test_calculate_stock_metrics(self, mock_yf, mock_upstox, mock_isin):
        mock_isin.return_value = "INE002A01018"
        mock_upstox.return_value = {
            "pe": 30.0,
            "sector_pe": 20.0,
            "pb": 2.5,
            "sector_pb": 2.0,
            "ps": 2.0,
            "eps": 80.0,
        }
        mock_yf.return_value = {
            "pe": 29.5,
            "forward_pe": 24.0,
            "pb": 2.4,
            "ps": 1.9,
            "eps": 80.0,
            "ebit": 100000.0,
            "interest_expense": 20000.0,
            "interest_coverage": 5.0,
            "raw_info": {"trailingPE": 29.5},
        }

        payload = calculate_stock_metrics("RELIANCE")

        self.assertEqual(payload["symbol"], "RELIANCE")
        self.assertEqual(payload["isin"], "INE002A01018")

        val = payload["valuation"]
        self.assertEqual(val["pe"], 30.0)
        self.assertEqual(val["sector_pe"], 20.0)
        self.assertEqual(val["pe_vs_sector_pe"], 1.5)  # 30 / 20 = 1.5
        self.assertEqual(val["pb"], 2.5)
        self.assertEqual(val["sector_pb"], 2.0)
        self.assertEqual(val["pb_vs_sector_pb"], 1.25)  # 2.5 / 2.0 = 1.25
        self.assertEqual(val["eps"], 80.0)
        self.assertEqual(val["fair_price"], 2400.0)  # 80.0 * 30.0 = 2400.0
        self.assertEqual(val["price_to_sales"], 2.0)

        sol = payload["solvency"]
        self.assertEqual(sol["interest_coverage_ratio"], 5.0)
        self.assertEqual(sol["ebit"], 100000.0)
        self.assertEqual(sol["interest_expense"], 20000.0)

    def test_isin_resolver_cached(self):
        _ISIN_CACHE["TESTSTOCK"] = "INE123456789"
        self.assertEqual(get_isin_for_symbol("TESTSTOCK"), "INE123456789")
        self.assertEqual(get_isin_for_symbol("TESTSTOCK.NS"), "INE123456789")


if __name__ == "__main__":
    unittest.main()
