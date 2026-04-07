from flask import Flask, request, jsonify, render_template
import yfinance as yf
import pandas as pd
from datetime import datetime, date
import io

app = Flask(__name__)


def get_ytd_start():
    today = date.today()
    return date(today.year, 1, 1)


def fetch_performance(ticker: str, weight_pct: float) -> dict:
    """weight_pct is already in percent (e.g. 20.0 for 20%)."""
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(start=get_ytd_start().strftime("%Y-%m-%d"), auto_adjust=True)

        if hist.empty:
            return {"ticker": ticker, "weight": weight_pct, "error": "No data found"}

        closes = hist["Close"].dropna()

        if len(closes) < 2:
            return {"ticker": ticker, "weight": weight_pct, "error": "Insufficient data"}

        current = closes.iloc[-1]

        def pct_change(past_price):
            return round(((current - past_price) / past_price) * 100, 2)

        day_return   = pct_change(closes.iloc[-2])
        week_idx     = max(0, len(closes) - 6)
        week_return  = pct_change(closes.iloc[week_idx])
        month_idx    = max(0, len(closes) - 22)
        month_return = pct_change(closes.iloc[month_idx])
        ytd_return   = pct_change(closes.iloc[0])

        name = ticker
        try:
            name = stock.info.get("shortName", ticker)
        except Exception:
            pass

        return {
            "ticker": ticker,
            "name": name,
            "weight": round(float(weight_pct), 4),
            "current_price": round(float(current), 2),
            "day": day_return,
            "week": week_return,
            "month": month_return,
            "ytd": ytd_return,
        }
    except Exception as e:
        return {"ticker": ticker, "weight": round(float(weight_pct), 4), "error": str(e)}


def compute_portfolio_returns(positions: list) -> dict:
    portfolio = {"day": 0.0, "week": 0.0, "month": 0.0, "ytd": 0.0}
    total_w   = {"day": 0.0, "week": 0.0, "month": 0.0, "ytd": 0.0}

    for pos in positions:
        if "error" in pos:
            continue
        w = pos["weight"] / 100.0
        for period in ["day", "week", "month", "ytd"]:
            val = pos.get(period)
            if val is not None:
                portfolio[period] += w * val
                total_w[period]   += w

    result = {}
    for period in ["day", "week", "month", "ytd"]:
        tw = total_w[period]
        result[period] = round(portfolio[period] / tw, 2) if tw > 0 else None

    return result


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    """Parse an Excel file and return the positions list (no market data)."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if not file.filename.endswith((".xlsx", ".xls")):
        return jsonify({"error": "File must be an Excel file (.xlsx or .xls)"}), 400

    try:
        df = pd.read_excel(io.BytesIO(file.read()))
    except Exception as e:
        return jsonify({"error": f"Could not parse Excel file: {e}"}), 400

    df.columns = [c.strip().lower() for c in df.columns]

    ticker_col = next((c for c in df.columns if "ticker" in c or "symbol" in c), None)
    weight_col = next((c for c in df.columns if "weight" in c or "allocation" in c or "%" in c), None)

    if ticker_col is None:
        return jsonify({"error": "Could not find a 'Ticker' or 'Symbol' column"}), 400
    if weight_col is None:
        return jsonify({"error": "Could not find a 'Weight', 'Allocation', or '%' column"}), 400

    df = df[[ticker_col, weight_col]].dropna()
    df.columns = ["ticker", "weight"]
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["weight"] = pd.to_numeric(df["weight"], errors="coerce")
    df = df.dropna()

    weight_sum = df["weight"].sum()
    if weight_sum <= 1.5:          # decimals → convert to percent
        df["weight"] = df["weight"] * 100

    positions = [
        {"ticker": row["ticker"], "weight": round(float(row["weight"]), 4)}
        for _, row in df.iterrows()
    ]

    return jsonify({"positions": positions})


@app.route("/analyze", methods=["POST"])
def analyze():
    """Fetch market data for a JSON list of {ticker, weight} positions."""
    data = request.get_json(silent=True)
    if not data or "positions" not in data:
        return jsonify({"error": "Expected JSON body with 'positions' array"}), 400

    raw = data["positions"]
    if not raw:
        return jsonify({"error": "No positions provided"}), 400

    # Validate and normalise weights
    positions_in = []
    for item in raw:
        ticker = str(item.get("ticker", "")).strip().upper()
        try:
            weight = float(item.get("weight", 0))
        except (TypeError, ValueError):
            continue
        if ticker and weight > 0:
            positions_in.append({"ticker": ticker, "weight": weight})

    if not positions_in:
        return jsonify({"error": "No valid positions found"}), 400

    # If weights look like decimals, convert to percent
    total = sum(p["weight"] for p in positions_in)
    if total <= 1.5:
        for p in positions_in:
            p["weight"] = p["weight"] * 100

    results = [fetch_performance(p["ticker"], p["weight"]) for p in positions_in]
    portfolio   = compute_portfolio_returns(results)
    total_weight = round(sum(p["weight"] for p in positions_in), 2)

    return jsonify({
        "positions":    results,
        "portfolio":    portfolio,
        "total_weight": total_weight,
        "as_of":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


if __name__ == "__main__":
    app.run(debug=True, port=5050)
