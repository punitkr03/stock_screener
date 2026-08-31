"""
fetcher.py
----------
Data connectors for stock fundamental metrics:
1. Upstox Company Fundamentals API (Key Ratios).
2. Yahoo Finance (financial statements and valuation multiples).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests
import yfinance as yf

from config import UPSTOX_AUTH_TOKEN

log = logging.getLogger(__name__)


def fetch_upstox_key_ratios(isin: str, auth_token: str | None = None) -> Optional[dict[str, Any]]:
    """
    Fetch key ratios and sector valuation metrics from Upstox Fundamentals API.
    
    Endpoint:
        GET https://api.upstox.com/v2/fundamentals/{isin}/key-ratios
        
    Returns
    -------
    dict | None
        Parsed response dictionary or None if request fails.
    """
    token = auth_token or UPSTOX_AUTH_TOKEN
    if not token:
        log.debug("No UPSTOX_AUTH_TOKEN configured, skipping Upstox key-ratios call for %s", isin)
        return None

    url = f"https://api.upstox.com/v2/fundamentals/{isin}/key-ratios"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success" and "data" in data:
                return data["data"]
            return data
        else:
            log.warning("Upstox key-ratios failed for ISIN %s: HTTP %d - %s", isin, resp.status_code, resp.text[:200])
            return None
    except Exception as exc:
        log.warning("Upstox key-ratios request error for ISIN %s: %s", isin, exc)
        return None


def fetch_yfinance_data(symbol: str) -> dict[str, Any]:
    """
    Fetch valuation and financial statement data via yfinance.
    
    Returns
    -------
    dict
        Dictionary containing extracted info, financials, EBIT, interest expense,
        and interest coverage ratio.
    """
    clean_sym = symbol.strip().upper().replace(".NS", "")
    ticker_str = f"{clean_sym}.NS"
    result: dict[str, Any] = {
        "pe": None,
        "forward_pe": None,
        "pb": None,
        "ps": None,
        "eps": None,
        "ebit": None,
        "interest_expense": None,
        "interest_coverage": None,
        "raw_info": {},
    }

    try:
        stock = yf.Ticker(ticker_str)
        info = stock.info or {}
        result["raw_info"] = info

        result["pe"] = info.get("trailingPE")
        result["forward_pe"] = info.get("forwardPE")
        result["pb"] = info.get("priceToBook")
        result["ps"] = info.get("priceToSalesTrailing12Months")
        result["eps"] = info.get("trailingEps") or info.get("forwardEps")

        # Extract financial statements for Interest Coverage Ratio
        financials = None
        try:
            financials = stock.financials
            if financials is None or financials.empty:
                financials = stock.income_stmt
        except Exception:
            pass

        if financials is not None and not financials.empty:
            ebit = None
            interest_expense = None

            # Try EBIT or Operating Income
            for key in ["EBIT", "Operating Income", "Operating Revenue"]:
                if key in financials.index:
                    try:
                        val = float(financials.loc[key].dropna().iloc[0])
                        ebit = val
                        break
                    except (IndexError, ValueError, TypeError):
                        continue

            # Try Interest Expense
            for key in ["Interest Expense", "Interest Expense Non Operating", "Net Non Operating Interest Income Expense"]:
                if key in financials.index:
                    try:
                        val = abs(float(financials.loc[key].dropna().iloc[0]))
                        interest_expense = val
                        break
                    except (IndexError, ValueError, TypeError):
                        continue

            result["ebit"] = ebit
            result["interest_expense"] = interest_expense

            if ebit is not None and interest_expense is not None and interest_expense > 0:
                result["interest_coverage"] = ebit / interest_expense

    except Exception as exc:
        log.warning("yfinance fetch failed for %s: %s", ticker_str, exc)

    return result
