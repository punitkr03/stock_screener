"""
isin_resolver.py
----------------
Utility to resolve NSE stock symbols to their corresponding ISIN (International
Securities Identification Number), required by the Upstox Fundamentals API.

Resolution strategies:
1. In-memory cache for fast repeated lookups.
2. Local symbols/master files if available.
3. yfinance Ticker.isin property fallback.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
from pathlib import Path
from typing import Optional

import yfinance as yf

from config import DATA_DIR, SYMBOLS_CSV

log = logging.getLogger(__name__)

# In-memory lookup cache: {"RELIANCE": "INE002A01018"}
_ISIN_CACHE: dict[str, str] = {}
_SYMBOLS_LOADED = False


def _load_symbols_csv_cache() -> None:
    """Load symbol-to-ISIN mappings from symbols.csv or NSE archives."""
    global _SYMBOLS_LOADED
    if _SYMBOLS_LOADED:
        return

    _SYMBOLS_LOADED = True
    csv_path = Path(SYMBOLS_CSV)

    if csv_path.exists():
        try:
            import pandas as pd
            df = pd.read_csv(csv_path)
            cols = [c.strip().upper() for c in df.columns]
            df.columns = cols
            if "SYMBOL" in df.columns and "ISIN" in df.columns:
                for _, row in df.iterrows():
                    sym = str(row.get("SYMBOL", "")).strip().upper()
                    isin = str(row.get("ISIN", "")).strip().upper()
                    if sym and isin and isin != "NAN" and len(isin) == 12:
                        _ISIN_CACHE[sym] = isin
            if _ISIN_CACHE:
                log.info("Loaded %d symbol-to-ISIN mappings from %s", len(_ISIN_CACHE), csv_path.name)
                return
        except Exception as exc:
            log.debug("Could not load ISIN from %s: %s", csv_path, exc)

    # If symbols.csv did not have ISINs, fetch from official NSE archives directly
    try:
        import io
        import requests
        import pandas as pd
        url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = [c.strip().upper() for c in df.columns]
            for _, row in df.iterrows():
                sym = str(row.get("SYMBOL", "")).strip().upper()
                isin = str(row.get("ISIN NUMBER", "")).strip().upper()
                if sym and isin and isin != "NAN" and len(isin) == 12:
                    _ISIN_CACHE[sym] = isin
            log.info("Fetched %d symbol-to-ISIN mappings from NSE archives.", len(_ISIN_CACHE))
    except Exception as exc:
        log.debug("Could not fetch ISINs from NSE archives: %s", exc)


def get_isin_for_symbol(symbol: str) -> Optional[str]:
    """
    Resolve an NSE equity symbol to an ISIN.
    
    Parameters
    ----------
    symbol : str
        NSE symbol (e.g. 'RELIANCE', 'TCS', 'INFY')
        
    Returns
    -------
    str | None
        12-character ISIN or None if not resolvable.
    """
    clean_sym = symbol.strip().upper().replace(".NS", "")
    
    # 1. Check in-memory cache
    if clean_sym in _ISIN_CACHE:
        return _ISIN_CACHE[clean_sym]
        
    # 2. Check local symbols cache if not loaded yet
    _load_symbols_csv_cache()
    if clean_sym in _ISIN_CACHE:
        return _ISIN_CACHE[clean_sym]
        
    # 3. Fallback to yfinance ticker metadata
    try:
        ticker = yf.Ticker(f"{clean_sym}.NS")
        isin = ticker.isin
        if isin and isinstance(isin, str) and len(isin.strip()) == 12 and isin.strip() != "-":
            isin_val = isin.strip().upper()
            _ISIN_CACHE[clean_sym] = isin_val
            return isin_val
    except Exception as exc:
        log.debug("yfinance ISIN lookup failed for %s: %s", clean_sym, exc)

    return None
