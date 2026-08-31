"""
engine.py
---------
Core metrics calculator combining Upstox Key Ratios and Yahoo Finance financials:
1. EPS & PE -> Fair Price (EPS * PE)
2. Price-to-Sales (P/S)
3. Interest Coverage Ratio (EBIT / Interest Expense)
4. PB vs Sector PB and PE vs Sector PE comparisons
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Optional

from analytics.fetcher import fetch_upstox_key_ratios, fetch_yfinance_data
from analytics.isin_resolver import get_isin_for_symbol

log = logging.getLogger(__name__)


def _to_float(val: Any) -> Optional[float]:
    """Convert input to float safely, returning None if invalid."""
    if val is None:
        return None
    try:
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            clean = val.replace(",", "").replace("%", "").strip()
            if clean in ("", "-", "N/A", "NA", "null", "None"):
                return None
            return float(clean)
    except (ValueError, TypeError):
        pass
    return None


def _parse_upstox_ratios(raw_data: Any) -> dict[str, Any]:
    """
    Parse Upstox key-ratios response into normalized fields.
    Handles dict, list of ratio objects, or nested structures.
    """
    extracted: dict[str, Any] = {
        "pe": None,
        "sector_pe": None,
        "pb": None,
        "sector_pb": None,
        "ps": None,
        "sector_ps": None,
        "eps": None,
        "roe": None,
        "roce": None,
        "ev_to_ebitda": None,
    }

    if not raw_data:
        return extracted

    items: list[dict] = []
    if isinstance(raw_data, list):
        items = [i for i in raw_data if isinstance(i, dict)]
    elif isinstance(raw_data, dict):
        if "ratios" in raw_data and isinstance(raw_data["ratios"], list):
            items = [i for i in raw_data["ratios"] if isinstance(i, dict)]
        elif "data" in raw_data and isinstance(raw_data["data"], list):
            items = [i for i in raw_data["data"] if isinstance(i, dict)]
        else:
            # Direct dictionary mapping
            extracted["pe"] = _to_float(raw_data.get("pe") or raw_data.get("pe_ratio") or raw_data.get("trailing_pe"))
            extracted["sector_pe"] = _to_float(raw_data.get("sector_pe") or raw_data.get("industry_pe"))
            extracted["pb"] = _to_float(raw_data.get("pb") or raw_data.get("pb_ratio") or raw_data.get("price_to_book"))
            extracted["sector_pb"] = _to_float(raw_data.get("sector_pb") or raw_data.get("industry_pb"))
            extracted["ps"] = _to_float(raw_data.get("ps") or raw_data.get("price_to_sales") or raw_data.get("ps_ratio"))
            extracted["sector_ps"] = _to_float(raw_data.get("sector_ps") or raw_data.get("industry_ps"))
            extracted["eps"] = _to_float(raw_data.get("eps") or raw_data.get("ttm_eps"))
            return extracted

    # Parse list of ratio objects
    import re
    for item in items:
        name = str(item.get("name") or item.get("ratio_name") or item.get("indicator") or item.get("field") or "").upper().strip()
        comp_val = _to_float(item.get("company") or item.get("value") or item.get("company_value") or item.get("companyValue"))
        sec_val = _to_float(item.get("sector") or item.get("sector_value") or item.get("industry_value") or item.get("sectorValue") or item.get("sector_average"))

        if "EPS" in name or "EARNINGS PER SHARE" in name:
            if extracted["eps"] is None:
                extracted["eps"] = comp_val
        elif "P/E" in name or "PRICE TO EARNING" in name or "PRICE/EARNING" in name or re.search(r"\bPE\b", name):
            if extracted["pe"] is None:
                extracted["pe"] = comp_val
            if extracted["sector_pe"] is None:
                extracted["sector_pe"] = sec_val
        elif "P/B" in name or "PRICE TO BOOK" in name or "PRICE/BOOK" in name or re.search(r"\bPB\b", name):
            if extracted["pb"] is None:
                extracted["pb"] = comp_val
            if extracted["sector_pb"] is None:
                extracted["sector_pb"] = sec_val
        elif "P/S" in name or "PRICE TO SALES" in name or "PRICE/SALES" in name or re.search(r"\bPS\b", name):
            if extracted["ps"] is None:
                extracted["ps"] = comp_val
            if extracted["sector_ps"] is None:
                extracted["sector_ps"] = sec_val
        elif "ROE" in name or "RETURN ON EQUITY" in name:
            if extracted["roe"] is None:
                extracted["roe"] = comp_val
        elif "ROCE" in name or "RETURN ON CAPITAL EMPLOYED" in name:
            if extracted["roce"] is None:
                extracted["roce"] = comp_val
        elif "EV/EBITDA" in name or "ENTERPRISE VALUE" in name:
            if extracted["ev_to_ebitda"] is None:
                extracted["ev_to_ebitda"] = comp_val

    return extracted


def calculate_stock_metrics(
    symbol: str,
    isin: str | None = None,
    auth_token: str | None = None,
) -> dict[str, Any]:
    """
    Calculate and aggregate valuation & financial metrics for a single stock symbol.
    
    Parameters
    ----------
    symbol : str
        NSE symbol (e.g. 'RELIANCE')
    isin : str | None
        Optional ISIN override. If not provided, will be resolved automatically.
    auth_token : str | None
        Optional Upstox Bearer token override.
        
    Returns
    -------
    dict
        Structured metrics payload ready for DB storage in metrics_data column.
    """
    clean_sym = symbol.strip().upper().replace(".NS", "")
    resolved_isin = isin or get_isin_for_symbol(clean_sym)

    # 1. Fetch from Upstox
    upstox_raw = fetch_upstox_key_ratios(resolved_isin, auth_token) if resolved_isin else None
    upstox_ratios = _parse_upstox_ratios(upstox_raw) if upstox_raw else {}

    # 2. Fetch from Yahoo Finance
    yf_data = fetch_yfinance_data(clean_sym)

    # 3. Consolidate Metrics (Upstox primary for sector ratios, yfinance fallback)
    pe = upstox_ratios.get("pe") if upstox_ratios.get("pe") is not None else yf_data.get("pe")
    sector_pe = upstox_ratios.get("sector_pe")
    forward_pe = yf_data.get("forward_pe")

    pb = upstox_ratios.get("pb") if upstox_ratios.get("pb") is not None else yf_data.get("pb")
    sector_pb = upstox_ratios.get("sector_pb")

    ps = upstox_ratios.get("ps") if upstox_ratios.get("ps") is not None else yf_data.get("ps")
    eps = upstox_ratios.get("eps") if upstox_ratios.get("eps") is not None else yf_data.get("eps")

    # Metric 1: Fair price = EPS * PE
    fair_price = round(eps * pe, 2) if (eps is not None and pe is not None) else None

    # Metric 3: Interest Coverage Ratio = EBIT / Interest Expense
    ebit = yf_data.get("ebit")
    interest_expense = yf_data.get("interest_expense")
    interest_coverage = yf_data.get("interest_coverage")
    if interest_coverage is not None:
        interest_coverage = round(interest_coverage, 2)

    # Metric 4: Comparisons
    pe_vs_sector_pe = None
    if pe is not None and sector_pe is not None and sector_pe > 0:
        pe_vs_sector_pe = round(pe / sector_pe, 2)

    pb_vs_sector_pb = None
    if pb is not None and sector_pb is not None and sector_pb > 0:
        pb_vs_sector_pb = round(pb / sector_pb, 2)

    payload = {
        "calculated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "symbol": clean_sym,
        "isin": resolved_isin,
        "valuation": {
            "pe": round(pe, 2) if pe is not None else None,
            "sector_pe": round(sector_pe, 2) if sector_pe is not None else None,
            "pe_vs_sector_pe": pe_vs_sector_pe,
            "forward_pe": round(forward_pe, 2) if forward_pe is not None else None,
            "pb": round(pb, 2) if pb is not None else None,
            "sector_pb": round(sector_pb, 2) if sector_pb is not None else None,
            "pb_vs_sector_pb": pb_vs_sector_pb,
            "price_to_sales": round(ps, 2) if ps is not None else None,
            "eps": round(eps, 2) if eps is not None else None,
            "fair_price": fair_price,
        },
        "solvency": {
            "ebit": ebit,
            "interest_expense": interest_expense,
            "interest_coverage_ratio": interest_coverage,
        },
        "data_sources": {
            "upstox_api": bool(upstox_raw),
            "yfinance": bool(yf_data.get("raw_info")),
        },
    }

    return payload
