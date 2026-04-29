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

# ✅ 9通貨対応
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

TICK_SIZE = {
    "BTCUSDT":  0.1,
    "ETHUSDT":  0.01,
    "XRPUSDT":  0.0001,
    "SOLUSDT":  0.001,
    "DOGEUSDT": 0.00001,
    "BNBUSDT":  0.01,
    "SUIUSDT":  0.0001,
    "ADAUSDT":  0.0001,
    "BGBUSDT":  0.0001,
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
    """
    ✅ 修正：エラー40883対策
    ポジションが既にある場合はレバレッジ変更できないため
    エラーを無視して注文を続行する
    """
    path = "/api/v2/mix/account/set-leverage"
    for side in ["long", "short"]:
        try:
            body = json.dumps({
                "symbol":      symbol,
                "productType": "USDT-FUTURES",
                "marginCoin":  "USDT",
                "leverage":    str(leverage),
                "holdSide":    side
            })
            headers = get_headers("POST", path, body)
            result = requests.post(BASE_URL + path, headers=headers, data=body)
            data = result.json()
            if data.get("code") != "00000":
                print(f"⚠️ レバレッジ設定スキップ ({side}): {data.get('msg', '')} → 注文は続行")
        except Exception as e:
            print(f"⚠️ レバレッジ設定エラー ({side}): {e} → 注文は続行")

def place_order(symbol, side, trade_side, size_usdt, leverage=1):
    price = get_current_price(symbol)
    if not price:
        return {"error": "価格取得失敗"}
    raw_qty = size_usdt / price
    qty     = round_qty(raw_qty, symbol)
    min_qty = MIN_QTY.get(symbol, 0.001)
    if qty < min_qty:
        qty = min_qty
    print(f"注文: {symbol} {side} {trade_side} {qty} (${size_usdt})")
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
    result = response.json()
    print(f"決済({hold_side}): {result}")
    return result

def close_all_positions(symbol):
    cancel_all_orders(symbol)
    time.sleep(0.5)
    results = []
    for side in ["long", "short"]:
        r = close_positions_by_side(symbol, side)
        results.append(r)
    return results

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        if request.is_json:
            data = request.get_json()
        else:
            data = json.loads(request.data.decode('utf-8'))
        print(f"受信: {data}")
        if isinstance(data, (int, float)):
            return jsonify({"status": "ignored"}), 200
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "認証失敗"}), 403

        action   = data.get("action", "").lower()
        symbol   = data.get("symbol", "BTCUSDT")
        leverage = int(data.get("leverage", 1))
        grid     = int(data.get("grid", 1))

        if symbol not in ORDER_SIZE:
            return jsonify({"error": f"未対応: {symbol}"}), 400

        size_usdt = ORDER_SIZE.get(symbol, 10)

        # ✅ レバレッジ設定（エラーがあっても注文は続行）
        set_leverage(symbol, leverage)

        # ✅ ロングエントリー
        if action == "long":
            result = place_order(symbol, "buy", "open", size_usdt, leverage)
            print(f"✅ ロング: {symbol}")
            return jsonify({"status": "ロングエントリー", "result": result})

        # ✅ ショートエントリー
        elif action == "short":
            result = place_order(symbol, "sell", "open", size_usdt, leverage)
            print(f"✅ ショート: {symbol}")
            return jsonify({"status": "ショートエントリー", "result": result})

        # ✅ グリッド追加
        elif action == "grid_add":
            side = data.get("side", "buy")
            result = place_order(symbol, side, "open", size_usdt, leverage)
            print(f"✅ グリッド{grid}段目追加: {symbol}")
            return jsonify({"status": f"グリッド{grid}段目", "result": result})

        # ✅ ヘッジロング
        elif action == "hedge_long":
            hedge_size = size_usdt * grid
            result = place_order(symbol, "buy", "open", hedge_size, leverage)
            print(f"🛡 ヘッジロング: {symbol} {hedge_size}USDT")
            return jsonify({"status": "ヘッジロング（手動利確）", "result": result})

        # ✅ ヘッジショート
        elif action == "hedge_short":
            hedge_size = size_usdt * grid
            result = place_order(symbol, "sell", "open", hedge_size, leverage)
            print(f"🛡 ヘッジショート: {symbol} {hedge_size}USDT")
            return jsonify({"status": "ヘッジショート（手動利確）", "result": result})

        # ✅ ショート決済
        elif action == "close_short":
            result = close_positions_by_side(symbol, "short")
            print(f"💰 ショート利確: {symbol}")
            return jsonify({"status": "ショート利確", "result": result})

        # ✅ ロング決済
        elif action == "close_long":
            result = close_positions_by_side(symbol, "long")
            print(f"💰 ロング利確: {symbol}")
            return jsonify({"status": "ロング利確", "result": result})

        # ✅ 全決済
        elif action in ["close", "close_all"]:
            results = close_all_positions(symbol)
            reason = data.get("reason", "")
            print(f"💰 全決済: {symbol} {reason}")
            return jsonify({"status": f"全決済({reason})", "result": results})

        else:
            return jsonify({"error": f"不明: {action}"}), 400

    except Exception as e:
        print(f"エラー: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status":  "稼働中",
        "message": "Bitget Bot - 9通貨対応（エラー40883修正済み）",
        "symbols": list(ORDER_SIZE.keys()),
        "order_sizes": ORDER_SIZE
    })

@app.route("/status", methods=["GET"])
def status():
    return jsonify({
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
