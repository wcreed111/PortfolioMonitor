import openpyxl

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Portfolio"

ws.append(["Ticker", "Weight"])
holdings = [
    ("AAPL", 0.20),
    ("MSFT", 0.18),
    ("GOOGL", 0.15),
    ("AMZN", 0.12),
    ("NVDA", 0.10),
    ("META", 0.08),
    ("JPM",  0.07),
    ("V",    0.05),
    ("BRK-B",0.03),
    ("SPY",  0.02),
]

for ticker, weight in holdings:
    ws.append([ticker, weight])

wb.save("sample_portfolio.xlsx")
print("Created sample_portfolio.xlsx")
