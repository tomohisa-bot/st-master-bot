from flask import Flask, request, jsonify
import hmac
import hashlib
import time
import requests
import json
import os
import base64

app = Flask(__name__)

BITGET_API_KEY    = os.environ.get("BITGET_API_KEY", "")
BITGET_SECRET_KEY = os.environ.get("BITGET_SECRET_KEY", "")
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "bitget_master_bot")

BASE_URL = "https://api.bitget.com"

# ✅ 9通貨対応（BNB追加）
ORDER_SIZE = {
    "BTCUSDT":  10,
    "ETHUSDT":  25,
    "XRPUSDT":  10,
    "SOLUSDT":  15,
    "DOGEUSDT": 10,
    "BNBUSDT":  10,
    "SUIUSDT":  10,
    "ADAUSDT":  10,
}

TICK_SIZE = {
    "BTCUSDT":  0.1,
    "ETHUSDT":  0.01,
    "XRPUSDT":  0.0001,
    "SOLUSDT":  0.001,
    "DOGEUSDT": 0.00001,
    "BNBUSDT":  0.01,
    "SUIUSDT":  0.0001,
    "ADAUSDT":  0.0001,
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
}

grid_state = {symbol: {"position": "none", "grid_count": 0, "entry_price": 0}
              for symbol in ORDER_SIZE}

def round_price(price, symbol):
    tick = TICK_SIZE.get(symbol, 0.1)
    return round(round(price / tick) * tick, 10)

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

def set_leverage(symbol, leverage):
    path = "/api/v2/mix/account/set-leverage"
    for side in ["long", "short"]:
        body = json.dumps({
            "symbol":      symbol,
            "productType": "USDT-FUTURES",
            "marginCoin":  "USDT",
            "leverage":    str(leverage),
            "holdSide":    side
        })
        headers = get_headers("POST", path, body)
        requests.post(BASE_URL + path, headers=headers, data=body)

def get_current_position(symbol):
    path = f"/api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT"
    headers = get_headers("GET", path)
    response = requests.get(BASE_URL + path, headers=headers)
    data = response.json()
    if data.get("code") != "00000" or not data.get("data"):
        return "none"
    for pos in data["data"]:
        if pos.get("symbol") != symbol:
            continue
        total = float(pos.get("total", 0))
        if total > 0:
            return pos["holdSide"]
    return "none"

def cancel_all_orders(symbol):
    path = "/api/v2/mix/order/cancel-all-orders"
    body = json.dumps({
        "symbol":      symbol,
        "productType": "USDT-FUTURES",
        "marginCoin":  "USDT"
    })
    headers = get_headers("POST", path, body)
    response = requests.post(BASE_URL + path, headers=headers, data=body)
    result = response.json()
    print(f"注文キャンセル結果: {result}")
    return result

def place_order(symbol, side, size_usdt, leverage=5):
    price = get_current_price(symbol)
    if not price:
        return {"error": "価格取得失敗"}
    raw_qty = size_usdt / price
    qty     = round_qty(raw_qty, symbol)
    min_qty = MIN_QTY.get(symbol, 0.001)
    if qty < min_qty:
        qty = min_qty
    print(f"注文: {symbol} {side} {qty} (${size_usdt} / {price})")
    path = "/api/v2/mix/order/place-order"
    order_body = {
        "symbol":      symbol,
        "productType": "USDT-FUTURES",
        "marginMode":  "isolated",
        "marginCoin":  "USDT",
        "size":        str(qty),
        "side":        side,
        "tradeSide":   "open",
        "orderType":   "market",
        "force":       "gtc"
    }
    body = json.dumps(order_body)
    headers = get_headers("POST", path, body)
    response = requests.post(BASE_URL + path, headers=headers, data=body)
    result = response.json()
    print(f"注文結果: {result}")
    return result

def close_all_positions(symbol):
    print(f"全決済開始: {symbol}")
    cancel_all_orders(symbol)
    time.sleep(0.8)
    flash_path = "/api/v2/mix/order/close-positions"
    results = []
    for hold_side in ["long", "short"]:
        flash_body = json.dumps({
            "symbol":      symbol,
            "productType": "USDT-FUTURES",
            "holdSide":    hold_side
        })
        flash_headers = get_headers("POST", flash_path, flash_body)
        flash_response = requests.post(BASE_URL + flash_path, headers=flash_headers, data=flash_body)
        flash_result = flash_response.json()
        print(f"flash-close({hold_side}): {flash_result}")
        if flash_result.get("code") == "00000":
            success_list = flash_result.get("data", {}).get("successList", [])
            if success_list:
                results.append(flash_result)
                print(f"✅ 決済成功: {symbol} {hold_side}")
    grid_state[symbol] = {"position": "none", "grid_count": 0, "entry_price": 0}
    return results if results else {"message": "決済するポジションなし"}

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        if request.is_json:
            data = request.get_json()
        else:
            data = json.loads(request.data.decode('utf-8'))
        print(f"受信データ: {data}")
        if isinstance(data, (int, float)):
            print("数値データを無視")
            return jsonify({"status": "ignored"}), 200
        if data.get("secret") != WEBHOOK_SECRET:
            print("認証失敗")
            return jsonify({"error": "認証失敗"}), 403
        action   = data.get("action", "").lower()
        symbol   = data.get("symbol", "BTCUSDT")
        leverage = int(data.get("leverage", 5))
        if symbol not in ORDER_SIZE:
            return jsonify({"error": f"未対応シンボル: {symbol}"}), 400
        size_usdt = ORDER_SIZE.get(symbol, 10)
        set_leverage(symbol, leverage)
        state = grid_state[symbol]
        print(f"グリッド状態[{symbol}]: {state}")

        if action == "long":
            current_pos = get_current_position(symbol)
            if current_pos != "none":
                print(f"既にポジションあり → スキップ")
                return jsonify({"status": "スキップ"})
            price = get_current_price(symbol)
            grid_state[symbol] = {"position": "long", "grid_count": 1, "entry_price": price}
            result = place_order(symbol, "buy", size_usdt, leverage)
            print(f"✅ グリッドロング1段目: {symbol}")
            return jsonify({"status": "グリッドロング1段目", "result": result})

        elif action == "short":
            current_pos = get_current_position(symbol)
            if current_pos != "none":
                print(f"既にポジションあり → スキップ")
                return jsonify({"status": "スキップ"})
            price = get_current_price(symbol)
            grid_state[symbol] = {"position": "short", "grid_count": 1, "entry_price": price}
            result = place_order(symbol, "sell", size_usdt, leverage)
            print(f"✅ グリッドショート1段目: {symbol}")
            return jsonify({"status": "グリッドショート1段目", "result": result})

        elif action == "grid_add":
            grid_num = int(data.get("grid", 2))
            if state["position"] == "none":
                return jsonify({"status": "スキップ（ポジションなし）"})
            side = "buy" if state["position"] == "long" else "sell"
            grid_state[symbol]["grid_count"] = grid_num
            result = place_order(symbol, side, size_usdt, leverage)
            print(f"✅ グリッド{grid_num}段目追加: {symbol}")
            return jsonify({"status": f"グリッド{grid_num}段目", "result": result})

        elif action == "close_all":
            reason = data.get("reason", "")
            print(f"全決済: {symbol} reason={reason}")
            result = close_all_positions(symbol)
            emoji  = "💰" if reason == "TP" else "🚨" if reason == "MAX_LOSS" else "🛑"
            print(f"{emoji} 全決済完了: {symbol} {reason}")
            return jsonify({"status": f"全決済完了({reason})", "result": result})

        else:
            return jsonify({"error": f"不明なアクション: {action}"}), 400

    except Exception as e:
        print(f"エラー: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status":  "稼働中",
        "message": "Grid Master Bot v1.3 running!",
        "version": "Grid Master v1.3 (9通貨対応)",
        "symbols": list(ORDER_SIZE.keys()),
        "order_sizes": ORDER_SIZE
    })

@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "grid_state":  grid_state,
        "order_sizes": ORDER_SIZE,
        "min_qty":     MIN_QTY
    })

@app.route("/price/<symbol>", methods=["GET"])
def check_price(symbol):
    price = get_current_price(symbol)
    return jsonify({"symbol": symbol, "price": price})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
