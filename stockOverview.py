import yfinance as yf
meta = yf.Ticker("META")
print(meta.info)
print("Company Sector:", meta.info['sector'])
print("P/E Ratio:", meta.info['trailingPE'])
print("Company Beta:", meta.info['beta'])
for key, value in meta.info.items():
    print(f"{key}: {value}")
data = meta.history(period="max")
print(data.to_string())