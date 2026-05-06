from flask import Flask, request, jsonify
import hmac
import hashlib
import time
import requests
import json
import os
import base64
from collections import deque

app = Flask(__name__)

BITGET_API_KEY    = os.environ.get("BITGET_API_KEY", "")
BITGET_SECRET_KEY = os.environ.get("BITGET_SECRET_KEY", "")
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "bitget_master_bot")

BASE_URL = "https://api.bitget.com"

# ===== MT5用シンボル別キュー =====
MT5_SYMBOLS = ["BTCUSD", "ETHUSD", "BTCXAU", "TRUMPUSD"]
mt5_queues = {symbol: deque(maxlen=100) for symbol in MT5_SYMBOLS}

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

MT5_LOT_SIZE = {
    "BTCUSD":   0.03,
    "ETHUSD":   0.03,
    "BTCXAU":   0.03,
    "TRUMPUSD": 0.01,
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
    response = requests.get(url)
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
    response = requests.post(BASE_URL + path, headers=headers, data=body)
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
    response = requests.post(BASE_URL + path, headers=headers, data=body)
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
    response = requests.post(BASE_URL + path, headers=headers, data=body)
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
    try:
        if request.is_json:
            data = request.get_json()
        else:
            data = json.loads(request.data.decode('utf-8'))
        if isinstance(data, (int, float)):
            return jsonify({"status": "ignored"}), 200
        if data.get("secret") != WEBHOOK_SECRET:
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
        return jsonify({"error": str(e)}), 500

# ===== Vantage MT5用Webhook（シンボル別キューに保存）=====
@app.route("/mt5order", methods=["POST"])
def mt5order():
    try:
        data = request.get_json()
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "認証失敗"}), 403

        action = data.get("action", "").lower()
        symbol = data.get("symbol", "BTCUSD")
        lots   = float(data.get("lots", MT5_LOT_SIZE.get(symbol, 0.01)))

        if action not in ["long", "short", "close_long", "close_short"]:
            return jsonify({"error": f"不明なaction: {action}"}), 400

        # 未登録シンボルは動的に追加
        if symbol not in mt5_queues:
            mt5_queues[symbol] = deque(maxlen=100)
            print(f"✅ 新規シンボルキュー作成: {symbol}")

        order = {
            "action": action,
            "symbol": symbol,
            "lots":   lots,
            "time":   int(time.time())
        }
        mt5_queues[symbol].append(order)
        print(f"✅ MT5キュー追加 [{symbol}]: {order}")

        return jsonify({"status": "OK", "order": order})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ===== MT5 EAがシンボル指定でポーリング =====
@app.route("/mt5poll", methods=["GET"])
def mt5poll():
    try:
        secret = request.args.get("secret", "")
        if secret != WEBHOOK_SECRET:
            return jsonify({"error": "認証失敗"}), 403

        symbol = request.args.get("symbol", "")
        if not symbol:
            return jsonify({"error": "symbolパラメータが必要です"}), 400

        if symbol not in mt5_queues:
            mt5_queues[symbol] = deque(maxlen=100)

        queue = mt5_queues[symbol]
        if len(queue) == 0:
            return jsonify({"status": "empty", "order": None})

        order = queue.popleft()
        print(f"📤 MT5へ送信 [{symbol}]: {order}")
        return jsonify({"status": "order", "order": order})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ===== ヘルスチェック =====
@app.route("/", methods=["GET"])
def health():
    queue_status = {sym: len(q) for sym, q in mt5_queues.items()}
    return jsonify({
        "status":    "稼働中",
        "message":   "Bitget + Vantage MT5 Bot",
        "mt5_queues": queue_status
    })

@app.route("/status", methods=["GET"])
def status():
    queue_status = {sym: len(q) for sym, q in mt5_queues.items()}
    return jsonify({
        "order_sizes": ORDER_SIZE,
        "mt5_queues":  queue_status
    })

@app.route("/price/<symbol>", methods=["GET"])
def check_price(symbol):
    price = get_current_price(symbol)
    return jsonify({"symbol": symbol, "price": price})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
