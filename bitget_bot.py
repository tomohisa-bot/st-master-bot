from flask import Flask, request, jsonify
import hmac
import hashlib
import time
import requests
import json
import os
import base64
import sqlite3
import math
import re
import tempfile
import uuid
import copy
import logging
from contextlib import contextmanager

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16384
VERSION = "3.4.1"

class RedactAccessSecrets(logging.Filter):
    def filter(self, record):
        def clean(value):
            if not isinstance(value, str):
                return value
            return re.sub(r"(?i)(secret=)[^&\s\"']+", r"\1[REDACTED]", value)
        record.msg = clean(record.msg)
        if isinstance(record.args, dict):
            args = copy.copy(record.args)
            for key, value in args.items():
                args[key] = "[REDACTED]" if "authorization" in str(key).lower() else clean(value)
            record.args = args
        elif isinstance(record.args, tuple):
            record.args = tuple(clean(value) for value in record.args)
        return True

# Keep request/status audit logs, but never publish legacy query credentials.
logging.getLogger("gunicorn.access").addFilter(RedactAccessSecrets())


BITGET_API_KEY    = os.environ.get("BITGET_API_KEY", "")
BITGET_SECRET_KEY = os.environ.get("BITGET_SECRET_KEY", "")
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET must be configured")
ENABLE_LEGACY_BITGET = os.environ.get("ENABLE_LEGACY_BITGET", "false").lower() == "true"
ALLOW_LEGACY_QUERY_AUTH = os.environ.get("ALLOW_LEGACY_QUERY_AUTH", "true").lower() == "true"
SIGNAL_TTL_SECONDS = int(os.environ.get("SIGNAL_TTL_SECONDS", "0"))
if SIGNAL_TTL_SECONDS < 0:
    raise RuntimeError("SIGNAL_TTL_SECONDS must be non-negative")

BASE_URL = "https://api.bitget.com"

# ===== MT5用シンボル別キュー =====
MT5_SYMBOLS = [
    "BTCUSD",   # 仮想通貨メイン
    "XAUUSD",   # ★追加：ゴールド
    "ETHUSD",   # 仮想通貨
    "DOGEUSD",  # 仮想通貨
    "SOLUSD",   # 仮想通貨
    "DOTUSD",   # 仮想通貨
    "XRPUSD",   # 仮想通貨
    "EURUSD",   # FX
    "AUDUSD",   # FX
    "GBPUSD",   # FX
    "USDJPY",   # FX
    "NAS100.r", # 株価指数
    "SP500ft.r",# 株価指数
    "GER40.r",  # 株価指数
    "JPN225ft", # 株価指数
]
MT5_SYMBOLS = set(filter(None, os.environ.get("MT5_ALLOWED_SYMBOLS", ",".join(MT5_SYMBOLS)).split(",")))
DB_PATH = os.environ.get("RELAY_DB_PATH", os.path.join(tempfile.gettempdir(), "mt5-relay-v340.sqlite3"))

@contextmanager
def db_connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    try:
        with db:
            yield db
    finally:
        db.close()

with db_connect() as db:
    db.execute("CREATE TABLE IF NOT EXISTS signals (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, symbol TEXT NOT NULL, strategy TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending')")
    db.execute("CREATE INDEX IF NOT EXISTS signals_pending ON signals(status,symbol,strategy,seq)")

def authenticated(body=None):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        supplied = auth[7:]
    elif isinstance(body, dict):
        supplied = body.get("secret", "")
    elif ALLOW_LEGACY_QUERY_AUTH:
        supplied = request.args.get("secret", "")
    else:
        supplied = ""
    return isinstance(supplied, str) and hmac.compare_digest(supplied.encode(), WEBHOOK_SECRET.encode())

def parse_object():
    try:
        data = json.loads(request.get_data(cache=False))
    except (ValueError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None

def queue_status():
    with db_connect() as db:
        return {symbol + ("|"+strategy if strategy else ""): count for symbol,strategy,count in db.execute("SELECT symbol,strategy,COUNT(*) FROM signals WHERE status='pending' GROUP BY symbol,strategy")}


ORDER_SIZE = {
    "BTCUSDT":  50,
    "ETHUSDT":  50,
    "XRPUSDT":  50,
    "SOLUSDT":  10,
    "DOGEUSDT": 10,
    "BNBUSDT":  50,
    "SUIUSDT":  10,
    "ADAUSDT":  10,
    "BGBUSDT":  10,
}

MIN_QTY = {
    "BTCUSDT":  0.001,
    "ETHUSDT":  0.01,
    "XRPUSDT":  1.0,
    "SOLUSDT":  0.1,
    "DOGEUSDT": 1.0,
    "BNBUSDT":  0.01,
    "SUIUSDT":  1.0,
    "ADAUSDT":  1.0,
    "BGBUSDT":  1.0,
}

QTY_DECIMALS = {
    "BTCUSDT":  3,
    "ETHUSDT":  2,
    "XRPUSDT":  0,
    "SOLUSDT":  1,
    "DOGEUSDT": 0,
    "BNBUSDT":  2,
    "SUIUSDT":  0,
    "ADAUSDT":  0,
    "BGBUSDT":  0,
}

# ===== MT5ロットサイズ =====
MT5_LOT_SIZE = {
    "BTCUSD":    0.03,   # ★稼ぎ頭：0.03ロット
    "XAUUSD":    0.01,   # ★追加：ゴールド
    "ETHUSD":    0.01,
    "DOGEUSD":   0.01,
    "SOLUSD":    0.01,
    "DOTUSD":    0.01,
    "XRPUSD":    0.001,
    "EURUSD":    0.01,
    "AUDUSD":    0.01,
    "GBPUSD":    0.01,
    "USDJPY":    0.01,
    "NAS100.r":  0.01,
    "SP500ft.r": 0.01,
    "GER40.r":   0.01,
    "JPN225ft":  0.01,
}

def round_qty(qty, symbol):
    decimals = QTY_DECIMALS.get(symbol, 3)
    return round(qty, decimals)

def generate_signature(timestamp, method, request_path, body=""):
    message = str(timestamp) + method.upper() + request_path + body
    signature = hmac.new(
        BITGET_SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).digest()
    return base64.b64encode(signature).decode("utf-8")

def get_headers(method, request_path, body=""):
    timestamp = str(int(time.time() * 1000))
    signature = generate_signature(timestamp, method, request_path, body)
    return {
        "ACCESS-KEY":        BITGET_API_KEY,
        "ACCESS-SIGN":       signature,
        "ACCESS-TIMESTAMP":  timestamp,
        "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
        "Content-Type":      "application/json",
        "locale":            "ja-JP"
    }

def get_current_price(symbol):
    url = f"{BASE_URL}/api/v2/mix/market/ticker?symbol={symbol}&productType=USDT-FUTURES"
    response = requests.get(url, timeout=10)
    data = response.json()
    if data.get("code") == "00000":
        return float(data["data"][0]["lastPr"])
    return None

def place_order(symbol, side, trade_side, size_usdt, leverage=1):
    price = get_current_price(symbol)
    if not price:
        return {"error": "価格取得失敗"}
    raw_qty = size_usdt / price
    qty = round_qty(raw_qty, symbol)
    min_qty = MIN_QTY.get(symbol, 0.001)
    if qty < min_qty:
        qty = min_qty
    path = "/api/v2/mix/order/place-order"
    body = json.dumps({
        "symbol":      symbol,
        "productType": "USDT-FUTURES",
        "marginMode":  "isolated",
        "marginCoin":  "USDT",
        "size":        str(qty),
        "side":        side,
        "tradeSide":   trade_side,
        "orderType":   "market",
        "force":       "gtc"
    })
    headers = get_headers("POST", path, body)
    response = requests.post(BASE_URL + path, headers=headers, data=body, timeout=10)
    result = response.json()
    print(f"注文結果: {result}")
    return result

def cancel_all_orders(symbol):
    path = "/api/v2/mix/order/cancel-all-orders"
    body = json.dumps({
        "symbol":      symbol,
        "productType": "USDT-FUTURES",
        "marginCoin":  "USDT"
    })
    headers = get_headers("POST", path, body)
    response = requests.post(BASE_URL + path, headers=headers, data=body, timeout=10)
    return response.json()

def close_positions_by_side(symbol, hold_side):
    cancel_all_orders(symbol)
    time.sleep(0.5)
    path = "/api/v2/mix/order/close-positions"
    body = json.dumps({
        "symbol":      symbol,
        "productType": "USDT-FUTURES",
        "holdSide":    hold_side
    })
    headers = get_headers("POST", path, body)
    response = requests.post(BASE_URL + path, headers=headers, data=body, timeout=10)
    return response.json()

def close_all_positions(symbol):
    cancel_all_orders(symbol)
    time.sleep(0.5)
    results = []
    for side in ["long", "short"]:
        r = close_positions_by_side(symbol, side)
        results.append(r)
    return results

# ===== Bitget用Webhook =====
@app.route("/webhook", methods=["POST"])
def webhook():
    if not ENABLE_LEGACY_BITGET:
        return jsonify({"error": "legacy_exchange_disabled"}), 410
    try:
        # ★修正：NullガードとJSONパース強化
        if request.is_json:
            data = request.get_json()
        else:
            raw = request.data.decode('utf-8').strip()
            if not raw:
                return jsonify({"error": "空のリクエスト"}), 400
            data = json.loads(raw)

        if data is None:
            return jsonify({"error": "JSONパース失敗"}), 400
        if isinstance(data, (int, float)):
            return jsonify({"status": "ignored"}), 200
        if not authenticated(data):
            return jsonify({"error": "認証失敗"}), 403

        action   = data.get("action", "").lower()
        symbol   = data.get("symbol", "BTCUSDT")
        leverage = int(data.get("leverage", 1))

        if symbol not in ORDER_SIZE:
            return jsonify({"error": f"未対応: {symbol}"}), 400

        size_usdt = ORDER_SIZE.get(symbol, 10)

        if action == "long":
            result = place_order(symbol, "buy", "open", size_usdt, leverage)
            return jsonify({"status": "ロングエントリー", "result": result})
        elif action == "short":
            result = place_order(symbol, "sell", "open", size_usdt, leverage)
            return jsonify({"status": "ショートエントリー", "result": result})
        elif action == "close_short":
            result = close_positions_by_side(symbol, "short")
            return jsonify({"status": "ショート利確", "result": result})
        elif action == "close_long":
            result = close_positions_by_side(symbol, "long")
            return jsonify({"status": "ロング利確", "result": result})
        elif action in ["close", "close_all"]:
            results = close_all_positions(symbol)
            return jsonify({"status": "全決済", "result": results})
        else:
            return jsonify({"error": f"不明: {action}"}), 400

    except Exception as e:
        print(f"❌ webhook error: {e}")
        return jsonify({"error": str(e)}), 500

# ===== MT5 relay: same v7.27 request/response format =====
@app.route("/mt5order", methods=["POST"])
def mt5order():
    data = parse_object()
    if data is None:
        return jsonify({"error": "JSON object required"}), 400
    if not authenticated(data):
        return jsonify({"error": "authentication_failed"}), 403
    action = data.get("action", "")
    symbol = data.get("symbol", "BTCUSD")
    strategy = data.get("strategy", "")
    if not isinstance(action, str) or action.lower() not in ("long", "short", "close_long", "close_short"):
        return jsonify({"error": "invalid_action"}), 400
    if not isinstance(symbol, str) or symbol not in MT5_SYMBOLS:
        return jsonify({"error": "symbol_not_allowed"}), 400
    if not isinstance(strategy, str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,48}", strategy):
        return jsonify({"error": "invalid_strategy"}), 400
    try:
        lots = float(data.get("lots", MT5_LOT_SIZE.get(symbol, 0.01)))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_lots"}), 400
    if isinstance(data.get("lots"), bool) or not math.isfinite(lots) or not 0 < lots <= 100:
        return jsonify({"error": "invalid_lots"}), 400
    supplied_id = data.get("id")
    if supplied_id is not None and (not isinstance(supplied_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", supplied_id)):
        return jsonify({"error": "invalid_id"}), 400
    now = time.time()
    event_id = hashlib.sha256(json.dumps([symbol,strategy,supplied_id]).encode()).hexdigest() if supplied_id else uuid.uuid4().hex
    order = {"action": action.lower(), "symbol": symbol, "lots": lots, "strategy": strategy, "time": int(now), "id": event_id}
    with db_connect() as db:
        db.execute("BEGIN IMMEDIATE")
        # Retain explicit IDs for 7 days; old pending signals are never silently discarded.
        db.execute("DELETE FROM signals WHERE status!='pending' AND created < ?", (now-7*86400,))
        if db.execute("SELECT 1 FROM signals WHERE event_id=?", (event_id,)).fetchone():
            return jsonify({"status": "duplicate", "id": event_id})
        scoped = db.execute("SELECT COUNT(*) FROM signals WHERE status='pending' AND symbol=? AND strategy=?", (symbol,strategy)).fetchone()[0]
        total = db.execute("SELECT COUNT(*) FROM signals WHERE status='pending'").fetchone()[0]
        if scoped >= 100 or total >= 1000:
            return jsonify({"error": "queue_full"}), 429
        db.execute("INSERT INTO signals(event_id,symbol,strategy,payload,created) VALUES(?,?,?,?,?)", (event_id,symbol,strategy,json.dumps(order),now))
    app.logger.info("signal queued id=%s symbol=%s action=%s", event_id,symbol,action.lower())
    return jsonify({"status": "OK", "order": order})

@app.route("/mt5poll", methods=["GET"])
def mt5poll():
    if not authenticated():
        return jsonify({"error": "authentication_failed"}), 403
    symbol = request.args.get("symbol", "")
    strategy = request.args.get("strategy", "")
    if symbol not in MT5_SYMBOLS or not re.fullmatch(r"[A-Za-z0-9_-]{0,48}", strategy):
        return jsonify({"error": "invalid_queue"}), 400
    with db_connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if SIGNAL_TTL_SECONDS:
            db.execute("UPDATE signals SET status='expired' WHERE status='pending' AND created < ?", (time.time()-SIGNAL_TTL_SECONDS,))
        row = db.execute("SELECT seq,payload FROM signals WHERE status='pending' AND symbol=? AND strategy=? ORDER BY seq LIMIT 1", (symbol,strategy)).fetchone()
        if row is None:
            return jsonify({"status": "empty", "order": None})
        # v7.27 has no ACK. Do not redeliver automatically: that could duplicate trades.
        db.execute("UPDATE signals SET status='delivered' WHERE seq=?", (row[0],))
    return jsonify({"status": "order", "order": json.loads(row[1])})

@app.route("/", methods=["GET"])
@app.route("/healthz", methods=["GET"])
def health():
    with db_connect() as db:
        db.execute("SELECT 1").fetchone()
    return jsonify({"status": "ok", "version": VERSION, "service": "mt5-relay"})

@app.route("/status", methods=["GET"])
def status():
    if not authenticated():
        return jsonify({"error": "authentication_failed"}), 403
    return jsonify({"version": VERSION, "mt5_queues": queue_status(), "mt5_lot_size": MT5_LOT_SIZE, "legacy_bitget_enabled": ENABLE_LEGACY_BITGET, "legacy_query_auth": ALLOW_LEGACY_QUERY_AUTH, "signal_ttl_seconds": SIGNAL_TTL_SECONDS, "storage": "sqlite; durability depends on hosting volume"})

@app.route("/price/<symbol>", methods=["GET"])
def check_price(symbol):
    if not ENABLE_LEGACY_BITGET:
        return jsonify({"error": "legacy_exchange_disabled"}), 410
    if not authenticated():
        return jsonify({"error": "authentication_failed"}), 403
    if symbol not in ORDER_SIZE:
        return jsonify({"error": "invalid_symbol"}), 400
    return jsonify({"symbol": symbol, "price": get_current_price(symbol)})

@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
