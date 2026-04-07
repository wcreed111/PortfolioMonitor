from flask import Flask, request, jsonify, render_template
import yfinance as yf
import pandas as pd
from datetime import datetime, date
import io, json, uuid
from pathlib import Path

app = Flask(__name__)

ALERTS_FILE = Path("alerts.json")


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_ytd_start():
    today = date.today()
    return date(today.year, 1, 1)


def fetch_performance(ticker: str, weight_pct: float) -> dict:
    """weight_pct is in percent (e.g. 20.0 for 20%)."""
    try:
        stock = yf.Ticker(ticker)
        hist  = stock.history(start=get_ytd_start().strftime("%Y-%m-%d"), auto_adjust=True)

        if hist.empty:
            return {"ticker": ticker, "weight": weight_pct, "error": "No data found"}

        closes = hist["Close"].dropna()
        if len(closes) < 2:
            return {"ticker": ticker, "weight": weight_pct, "error": "Insufficient data"}

        current = closes.iloc[-1]

        def pct_change(past):
            return round(((current - past) / past) * 100, 2)

        day_return   = pct_change(closes.iloc[-2])
        week_return  = pct_change(closes.iloc[max(0, len(closes) - 6)])
        month_return = pct_change(closes.iloc[max(0, len(closes) - 22)])
        ytd_return   = pct_change(closes.iloc[0])

        name = ticker
        try:
            name = stock.info.get("shortName", ticker)
        except Exception:
            pass

        return {
            "ticker":        ticker,
            "name":          name,
            "weight":        round(float(weight_pct), 4),
            "current_price": round(float(current), 2),
            "day":           day_return,
            "week":          week_return,
            "month":         month_return,
            "ytd":           ytd_return,
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

    return {
        period: round(portfolio[period] / total_w[period], 2) if total_w[period] > 0 else None
        for period in ["day", "week", "month", "ytd"]
    }


# ── Alerts persistence ────────────────────────────────────────────────────────

def load_alerts() -> list:
    if ALERTS_FILE.exists():
        try:
            return json.loads(ALERTS_FILE.read_text())
        except Exception:
            pass
    return []


def save_alerts(alerts: list):
    ALERTS_FILE.write_text(json.dumps(alerts, indent=2))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    """Parse an Excel file and return positions (no market data)."""
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

    if not ticker_col:
        return jsonify({"error": "Could not find a 'Ticker' or 'Symbol' column"}), 400
    if not weight_col:
        return jsonify({"error": "Could not find a 'Weight', 'Allocation', or '%' column"}), 400

    df = df[[ticker_col, weight_col]].dropna()
    df.columns = ["ticker", "weight"]
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["weight"] = pd.to_numeric(df["weight"], errors="coerce")
    df = df.dropna()

    if df["weight"].sum() <= 1.5:
        df["weight"] = df["weight"] * 100

    return jsonify({
        "positions": [
            {"ticker": row["ticker"], "weight": round(float(row["weight"]), 4)}
            for _, row in df.iterrows()
        ]
    })


@app.route("/analyze", methods=["POST"])
def analyze():
    """Fetch market data for a JSON list of {ticker, weight} positions."""
    data = request.get_json(silent=True)
    if not data or "positions" not in data:
        return jsonify({"error": "Expected JSON body with 'positions' array"}), 400

    positions_in = [
        {"ticker": str(p.get("ticker", "")).strip().upper(), "weight": float(p.get("weight", 0))}
        for p in data["positions"]
        if str(p.get("ticker", "")).strip() and float(p.get("weight", 0)) > 0
    ]

    if not positions_in:
        return jsonify({"error": "No valid positions found"}), 400

    if sum(p["weight"] for p in positions_in) <= 1.5:
        for p in positions_in:
            p["weight"] *= 100

    results      = [fetch_performance(p["ticker"], p["weight"]) for p in positions_in]
    portfolio    = compute_portfolio_returns(results)
    total_weight = round(sum(p["weight"] for p in positions_in), 2)

    return jsonify({
        "positions":    results,
        "portfolio":    portfolio,
        "total_weight": total_weight,
        "as_of":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


# ── Alert routes ──────────────────────────────────────────────────────────────

@app.route("/alerts", methods=["GET"])
def get_alerts():
    return jsonify(load_alerts())


@app.route("/alerts", methods=["POST"])
def add_alert():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    ticker    = str(data.get("ticker", "")).strip().upper()
    direction = data.get("direction", "either")   # "up" | "down" | "either"
    try:
        threshold = float(data.get("threshold", 0))
    except (TypeError, ValueError):
        threshold = 0

    if not ticker:
        return jsonify({"error": "Ticker is required"}), 400
    if threshold <= 0:
        return jsonify({"error": "Threshold must be greater than 0"}), 400
    if direction not in ("up", "down", "either"):
        return jsonify({"error": "Direction must be 'up', 'down', or 'either'"}), 400

    alerts = load_alerts()
    alert  = {
        "id":        str(uuid.uuid4()),
        "ticker":    ticker,
        "threshold": threshold,
        "direction": direction,
    }
    alerts.append(alert)
    save_alerts(alerts)
    return jsonify(alert), 201


@app.route("/alerts/<alert_id>", methods=["DELETE"])
def delete_alert(alert_id):
    alerts = [a for a in load_alerts() if a["id"] != alert_id]
    save_alerts(alerts)
    return jsonify({"ok": True})


@app.route("/alerts/check", methods=["GET"])
def check_alerts():
    alerts = load_alerts()
    if not alerts:
        return jsonify({"triggered": [], "checked": [], "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    # Fetch day return for each unique ticker once
    tickers    = list({a["ticker"] for a in alerts})
    day_returns = {}
    names       = {}
    prices      = {}
    for ticker in tickers:
        result = fetch_performance(ticker, 100.0)
        if "error" not in result:
            day_returns[ticker] = result.get("day")
            names[ticker]       = result.get("name", ticker)
            prices[ticker]      = result.get("current_price")
        else:
            day_returns[ticker] = None
            names[ticker]       = ticker
            prices[ticker]      = None

    triggered = []
    checked   = []

    for alert in alerts:
        day  = day_returns.get(alert["ticker"])
        item = {
            **alert,
            "name":          names.get(alert["ticker"], alert["ticker"]),
            "current_price": prices.get(alert["ticker"]),
            "day_return":    day,
        }
        checked.append(item)

        if day is None:
            continue

        d, t = alert["direction"], alert["threshold"]
        if   d == "up"     and day >=  t:   triggered.append(item)
        elif d == "down"   and day <= -t:   triggered.append(item)
        elif d == "either" and abs(day) >= t: triggered.append(item)

    return jsonify({
        "triggered": triggered,
        "checked":   checked,
        "as_of":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


if __name__ == "__main__":
    app.run(debug=True, port=5050)
