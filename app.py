import io
import json
import os
import sqlite3
import uuid
from datetime import datetime, date
from functools import wraps
from pathlib import Path

import pandas as pd
import yfinance as yf
from flask import (Flask, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)

# Stable secret key persisted to disk
_KEY_FILE = Path('.secret_key')
if _KEY_FILE.exists():
    app.secret_key = _KEY_FILE.read_bytes()
else:
    _key = os.urandom(32)
    _KEY_FILE.write_bytes(_key)
    app.secret_key = _key

DB_PATH = 'portfolio_monitor.db'


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('PRAGMA foreign_keys = ON')
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE NOT NULL COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS portfolios (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                name       TEXT NOT NULL,
                positions  TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id        TEXT PRIMARY KEY,
                user_id   INTEGER NOT NULL,
                ticker    TEXT NOT NULL,
                threshold REAL NOT NULL,
                direction TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)


# ── Auth helpers ──────────────────────────────────────────────────────────────

def require_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            if request.is_json or request.method != 'GET':
                return jsonify({'error': 'Not authenticated'}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated


def uid():
    return session['user_id']


# ── Market data ───────────────────────────────────────────────────────────────

def get_ytd_start():
    t = date.today()
    return date(t.year, 1, 1).strftime('%Y-%m-%d')


def fetch_performance(ticker: str, weight_pct: float) -> dict:
    try:
        stock = yf.Ticker(ticker)
        hist  = stock.history(start=get_ytd_start(), auto_adjust=True)

        if hist.empty:
            return {'ticker': ticker, 'weight': weight_pct, 'error': 'No data found'}

        closes = hist['Close'].dropna()
        if len(closes) < 2:
            return {'ticker': ticker, 'weight': weight_pct, 'error': 'Insufficient data'}

        current = closes.iloc[-1]

        def chg(past):
            return round(((current - past) / past) * 100, 2)

        name = ticker
        try:
            name = stock.info.get('shortName', ticker)
        except Exception:
            pass

        return {
            'ticker':        ticker,
            'name':          name,
            'weight':        round(float(weight_pct), 4),
            'current_price': round(float(current), 2),
            'day':           chg(closes.iloc[-2]),
            'week':          chg(closes.iloc[max(0, len(closes) - 6)]),
            'month':         chg(closes.iloc[max(0, len(closes) - 22)]),
            'ytd':           chg(closes.iloc[0]),
        }
    except Exception as e:
        return {'ticker': ticker, 'weight': round(float(weight_pct), 4), 'error': str(e)}


def compute_portfolio_returns(positions: list) -> dict:
    totals = {'day': 0.0, 'week': 0.0, 'month': 0.0, 'ytd': 0.0}
    weights = {'day': 0.0, 'week': 0.0, 'month': 0.0, 'ytd': 0.0}
    for pos in positions:
        if 'error' in pos:
            continue
        w = pos['weight'] / 100.0
        for p in totals:
            v = pos.get(p)
            if v is not None:
                totals[p]  += w * v
                weights[p] += w
    return {p: round(totals[p] / weights[p], 2) if weights[p] > 0 else None for p in totals}


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/login')
def login_page():
    if 'user_id' in session:
        return redirect(url_for('index'))
    return render_template('login.html')


@app.route('/login', methods=['POST'])
def do_login():
    d        = request.get_json(silent=True) or {}
    username = str(d.get('username', '')).strip()
    password = str(d.get('password', ''))
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'error': 'Invalid username or password'}), 401
    session.clear()
    session['user_id']  = user['id']
    session['username'] = user['username']
    return jsonify({'ok': True, 'username': user['username']})


@app.route('/register', methods=['POST'])
def do_register():
    d        = request.get_json(silent=True) or {}
    username = str(d.get('username', '')).strip()
    password = str(d.get('password', ''))
    if len(username) < 3:
        return jsonify({'error': 'Username must be at least 3 characters'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    db = get_db()
    try:
        db.execute(
            'INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)',
            (username, generate_password_hash(password, method='pbkdf2:sha256'), datetime.now().isoformat())
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Username already taken'}), 409
    user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    session.clear()
    session['user_id']  = user['id']
    session['username'] = user['username']
    return jsonify({'ok': True, 'username': user['username']}), 201


@app.route('/logout', methods=['POST'])
def do_logout():
    session.clear()
    return jsonify({'ok': True})


# ── Main ──────────────────────────────────────────────────────────────────────

@app.route('/')
@require_login
def index():
    return render_template('index.html', username=session.get('username'))


# ── Market data routes ────────────────────────────────────────────────────────

@app.route('/upload', methods=['POST'])
@require_login
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['file']
    if not file.filename.endswith(('.xlsx', '.xls')):
        return jsonify({'error': 'File must be an Excel file (.xlsx or .xls)'}), 400
    try:
        df = pd.read_excel(io.BytesIO(file.read()))
    except Exception as e:
        return jsonify({'error': f'Could not parse Excel file: {e}'}), 400

    df.columns = [c.strip().lower() for c in df.columns]
    tc = next((c for c in df.columns if 'ticker' in c or 'symbol' in c), None)
    wc = next((c for c in df.columns if 'weight' in c or 'allocation' in c or '%' in c), None)
    if not tc:
        return jsonify({'error': "Could not find a 'Ticker' or 'Symbol' column"}), 400
    if not wc:
        return jsonify({'error': "Could not find a 'Weight', 'Allocation', or '%' column"}), 400

    df = df[[tc, wc]].dropna()
    df.columns = ['ticker', 'weight']
    df['ticker'] = df['ticker'].astype(str).str.strip().str.upper()
    df['weight'] = pd.to_numeric(df['weight'], errors='coerce')
    df = df.dropna()
    if df['weight'].sum() <= 1.5:
        df['weight'] = df['weight'] * 100

    return jsonify({'positions': [
        {'ticker': r['ticker'], 'weight': round(float(r['weight']), 4)}
        for _, r in df.iterrows()
    ]})


@app.route('/analyze', methods=['POST'])
@require_login
def analyze():
    data = request.get_json(silent=True)
    if not data or 'positions' not in data:
        return jsonify({'error': "Expected JSON body with 'positions' array"}), 400
    raw = [
        {'ticker': str(p.get('ticker', '')).strip().upper(), 'weight': float(p.get('weight', 0))}
        for p in data['positions']
        if str(p.get('ticker', '')).strip() and float(p.get('weight', 0)) > 0
    ]
    if not raw:
        return jsonify({'error': 'No valid positions found'}), 400
    if sum(p['weight'] for p in raw) <= 1.5:
        for p in raw:
            p['weight'] *= 100
    results = [fetch_performance(p['ticker'], p['weight']) for p in raw]
    return jsonify({
        'positions':    results,
        'portfolio':    compute_portfolio_returns(results),
        'total_weight': round(sum(p['weight'] for p in raw), 2),
        'as_of':        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })


# ── Portfolio CRUD ────────────────────────────────────────────────────────────

@app.route('/portfolios', methods=['GET'])
@require_login
def list_portfolios():
    rows = get_db().execute(
        'SELECT id, name, positions, updated_at FROM portfolios WHERE user_id = ? ORDER BY updated_at DESC',
        (uid(),)
    ).fetchall()
    return jsonify([
        {'id': r['id'], 'name': r['name'],
         'positions': json.loads(r['positions']), 'updated_at': r['updated_at']}
        for r in rows
    ])


@app.route('/portfolios', methods=['POST'])
@require_login
def create_portfolio():
    d    = request.get_json(silent=True) or {}
    name = str(d.get('name', '')).strip()
    if not name:
        return jsonify({'error': 'Portfolio name is required'}), 400
    positions = d.get('positions', [])
    now = datetime.now().isoformat()
    db  = get_db()
    cur = db.execute(
        'INSERT INTO portfolios (user_id, name, positions, updated_at) VALUES (?, ?, ?, ?)',
        (uid(), name, json.dumps(positions), now)
    )
    db.commit()
    return jsonify({'id': cur.lastrowid, 'name': name, 'positions': positions, 'updated_at': now}), 201


@app.route('/portfolios/<int:pid>', methods=['PUT'])
@require_login
def update_portfolio(pid):
    db  = get_db()
    row = db.execute('SELECT id FROM portfolios WHERE id = ? AND user_id = ?', (pid, uid())).fetchone()
    if not row:
        return jsonify({'error': 'Portfolio not found'}), 404
    d         = request.get_json(silent=True) or {}
    name      = str(d.get('name', '')).strip() or None
    positions = d.get('positions')
    now       = datetime.now().isoformat()
    if name and positions is not None:
        db.execute('UPDATE portfolios SET name=?, positions=?, updated_at=? WHERE id=?',
                   (name, json.dumps(positions), now, pid))
    elif name:
        db.execute('UPDATE portfolios SET name=?, updated_at=? WHERE id=?', (name, now, pid))
    elif positions is not None:
        db.execute('UPDATE portfolios SET positions=?, updated_at=? WHERE id=?',
                   (json.dumps(positions), now, pid))
    db.commit()
    return jsonify({'ok': True, 'updated_at': now})


@app.route('/portfolios/<int:pid>', methods=['DELETE'])
@require_login
def delete_portfolio(pid):
    db  = get_db()
    row = db.execute('SELECT id FROM portfolios WHERE id = ? AND user_id = ?', (pid, uid())).fetchone()
    if not row:
        return jsonify({'error': 'Portfolio not found'}), 404
    db.execute('DELETE FROM portfolios WHERE id = ?', (pid,))
    db.commit()
    return jsonify({'ok': True})


# ── Alerts ────────────────────────────────────────────────────────────────────

@app.route('/alerts', methods=['GET'])
@require_login
def get_alerts():
    rows = get_db().execute('SELECT * FROM alerts WHERE user_id = ?', (uid(),)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/alerts', methods=['POST'])
@require_login
def add_alert():
    d         = request.get_json(silent=True) or {}
    ticker    = str(d.get('ticker', '')).strip().upper()
    direction = d.get('direction', 'either')
    try:
        threshold = float(d.get('threshold', 0))
    except (TypeError, ValueError):
        threshold = 0
    if not ticker:
        return jsonify({'error': 'Ticker is required'}), 400
    if threshold <= 0:
        return jsonify({'error': 'Threshold must be greater than 0'}), 400
    if direction not in ('up', 'down', 'either'):
        return jsonify({'error': "Direction must be 'up', 'down', or 'either'"}), 400
    alert_id = str(uuid.uuid4())
    db = get_db()
    db.execute('INSERT INTO alerts (id, user_id, ticker, threshold, direction) VALUES (?, ?, ?, ?, ?)',
               (alert_id, uid(), ticker, threshold, direction))
    db.commit()
    return jsonify({'id': alert_id, 'ticker': ticker, 'threshold': threshold, 'direction': direction}), 201


@app.route('/alerts/<alert_id>', methods=['DELETE'])
@require_login
def delete_alert(alert_id):
    db = get_db()
    db.execute('DELETE FROM alerts WHERE id = ? AND user_id = ?', (alert_id, uid()))
    db.commit()
    return jsonify({'ok': True})


@app.route('/alerts/check', methods=['GET'])
@require_login
def check_alerts():
    rows   = get_db().execute('SELECT * FROM alerts WHERE user_id = ?', (uid(),)).fetchall()
    alerts = [dict(r) for r in rows]
    if not alerts:
        return jsonify({'triggered': [], 'checked': [],
                        'as_of': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})

    tickers = list({a['ticker'] for a in alerts})
    day_ret, names, prices = {}, {}, {}
    for t in tickers:
        r = fetch_performance(t, 100.0)
        if 'error' not in r:
            day_ret[t] = r.get('day')
            names[t]   = r.get('name', t)
            prices[t]  = r.get('current_price')
        else:
            day_ret[t] = names[t] = None
            prices[t]  = None
            names[t]   = t

    triggered, checked = [], []
    for a in alerts:
        day  = day_ret.get(a['ticker'])
        item = {**a, 'name': names.get(a['ticker'], a['ticker']),
                'current_price': prices.get(a['ticker']), 'day_return': day}
        checked.append(item)
        if day is None:
            continue
        d, th = a['direction'], a['threshold']
        if   d == 'up'     and day >=  th:      triggered.append(item)
        elif d == 'down'   and day <= -th:      triggered.append(item)
        elif d == 'either' and abs(day) >= th:  triggered.append(item)

    return jsonify({'triggered': triggered, 'checked': checked,
                    'as_of': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5050))
    app.run(debug=False, host='0.0.0.0', port=port)
