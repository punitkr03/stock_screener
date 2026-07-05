import yfinance as yf

def main():
    data = yf.download(
            tickers="APOLLO.NS",
            period="5mo",
            interval="1d",
            auto_adjust=True,
            progress=False,
            group_by="ticker",
            threads=True,
        )
    print(data.columns)

if __name__ == "__main__":
    main()