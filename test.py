import pandas as pd
import yfinance as yf

TICKER = "RELIANCE.NS"
stock = yf.Ticker(TICKER)

info = stock.info
financials = stock.financials
balance_sheet = stock.balance_sheet

# 1. Valuation Multiples
pe_ratio = info.get("trailingPE")
pb_ratio = info.get("priceToBook")
ev_to_ebitda = info.get("enterpriseToEbitda")

# 2. Profitability & Returns (TTM / Latest Fiscal Year)
roe = info.get("returnOnEquity")
roa = info.get("returnOnAssets")
op_margin = info.get("operatingMargins")
net_margin = info.get("profitMargins")

# 3. Solvency & Coverage Ratios
try:
    ebit = (
        financials.loc["EBIT"].iloc[0]
        if "EBIT" in financials.index
        else financials.loc["Operating Income"].iloc[0]
    )
    interest_expense = abs(financials.loc["Interest Expense"].iloc[0])
    interest_coverage = ebit / interest_expense
except (KeyError, IndexError, ZeroDivisionError):
    interest_coverage = None

debt_to_equity = info.get("debtToEquity")  # Expressed as a percentage by yfinance
current_ratio = info.get("currentRatio")
quick_ratio = info.get("quickRatio")

# Display in a clean DataFrame
metrics = {
    "P/E Ratio": f"{pe_ratio:.2f}" if pe_ratio else "N/A",
    "P/B Ratio": f"{pb_ratio:.2f}" if pb_ratio else "N/A",
    "EV/EBITDA": f"{ev_to_ebitda:.2f}" if ev_to_ebitda else "N/A",
    "ROE": f"{roe * 100:.2f}%" if roe else "N/A",
    "ROA": f"{roa * 100:.2f}%" if roa else "N/A",
    "Operating Margin": f"{op_margin * 100:.2f}%" if op_margin else "N/A",
    "Net Profit Margin": f"{net_margin * 100:.2f}%" if net_margin else "N/A",
    "Interest Coverage Ratio": (
        f"{interest_coverage:.2f}x" if interest_coverage else "N/A"
    ),
    "Debt-to-Equity": f"{debt_to_equity / 100:.2f}" if debt_to_equity else "N/A",
    "Current Ratio": f"{current_ratio:.2f}" if current_ratio else "N/A",
    "Quick Ratio": f"{quick_ratio:.2f}" if quick_ratio else "N/A",
}

df_metrics = pd.DataFrame(
    list(metrics.items()), columns=["Metric", "Latest Value"]
)
print(f"=== Key Financial Metrics: {TICKER} ===")
print(df_metrics.to_string(index=False))