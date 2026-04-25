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

# 8通貨対応
ORDER_SIZE = {
    "BTCUSDT":  10,
    "ETHUSDT":  10,
    "XRPUSDT":  10,
    "SOLUSDT":  10,
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

# 状態管理
hedge_state = {symbol: {
    "short_count": 0,
    "long_count": 0,
    "hedge_active": False,
    "hedge_side": None,
} for symbol in ORDER_SIZE}

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

def place_order(symbol, side, trade_side, size_usdt, leverage=1):
    """
    side: buy or sell
    trade_side: open or close
    """
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
    order_body = {
        "symbol":      symbol,
        "productType": "USDT-FUTURES",
        "marginMode":  "isolated",
        "marginCoin":  "USDT",
        "size":        str(qty),
        "side":        side,
        "tradeSide":   trade_side,
        "orderType":   "market",
        "force":       "gtc"
    }

    body = json.dumps(order_body)
    headers = get_headers("POST", path, body)
    response = requests.post(BASE_URL + path, headers=headers, data=body)
    result = response.json()
    print(f"注文結果: {result}")
    return result

def close_positions_by_side(symbol, hold_side):
    """指定サイドのポジションをflash-closeで決済"""
    cancel_all_orders(symbol)
    time.sleep(0.5)

    flash_path = "/api/v2/mix/order/close-positions"
    flash_body = json.dumps({
        "symbol":      symbol,
        "productType": "USDT-FUTURES",
        "holdSide":    hold_side
    })
    flash_headers = get_headers("POST", flash_path, flash_body)
    flash_response = requests.post(BASE_URL + flash_path, headers=flash_headers, data=flash_body)
    result = flash_response.json()
    print(f"flash-close({hold_side}): {result}")
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
    result = response.json()
    print(f"注文キャンセル: {result}")
    return result

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
        set_leverage(symbol, leverage)

        state = hedge_state[symbol]
        print(f"状態[{symbol}]: {state}")

        # ✅ ショートエントリー（段階追加）
        if action == "short":
            result = place_order(symbol, "sell", "open", size_usdt, leverage)
            state["short_count"] = grid
            print(f"✅ ショート{grid}段目: {symbol}")
            return jsonify({"status": f"ショート{grid}段目", "result": result})

        # ✅ ロングエントリー（段階追加）
        elif action == "long":
            result = place_order(symbol, "buy", "open", size_usdt, leverage)
            state["long_count"] = grid
            print(f"✅ ロング{grid}段目: {symbol}")
            return jsonify({"status": f"ロング{grid}段目", "result": result})

        # ✅ ヘッジロング追加（ショート積み上げへのヘッジ）
        elif action == "hedge_long":
            # ショートの合計段階数分のUSDTをヘッジ
            hedge_size = size_usdt * state["short_count"]
            result = place_order(symbol, "buy", "open", hedge_size, leverage)
            state["hedge_active"] = True
            state["hedge_side"]   = "long"
            print(f"🛡 ヘッジロング追加: {symbol} {hedge_size}USDT（手動利確）")
            return jsonify({"status": "ヘッジロング追加（手動利確してください）", "result": result})

        # ✅ ヘッジショート追加（ロング積み上げへのヘッジ）
        elif action == "hedge_short":
            hedge_size = size_usdt * state["long_count"]
            result = place_order(symbol, "sell", "open", hedge_size, leverage)
            state["hedge_active"] = True
            state["hedge_side"]   = "short"
            print(f"🛡 ヘッジショート追加: {symbol} {hedge_size}USDT（手動利確）")
            return jsonify({"status": "ヘッジショート追加（手動利確してください）", "result": result})

        # ✅ ショート本体自動利確（ヘッジは除く）
        elif action == "close_short":
            result = close_positions_by_side(symbol, "short")
            state["short_count"] = 0
            print(f"💰 ショート本体自動利確: {symbol}")
            return jsonify({"status": "ショート本体利確完了（ヘッジは手動で）", "result": result})

        # ✅ ロング本体自動利確（ヘッジは除く）
        elif action == "close_long":
            result = close_positions_by_side(symbol, "long")
            state["long_count"] = 0
            print(f"💰 ロング本体自動利確: {symbol}")
            return jsonify({"status": "ロング本体利確完了（ヘッジは手動で）", "result": result})

        # 従来のclose（後方互換）
        elif action == "close":
            cancel_all_orders(symbol)
            time.sleep(0.5)
            results = []
            for side in ["long", "short"]:
                r = close_positions_by_side(symbol, side)
                results.append(r)
            state["short_count"]  = 0
            state["long_count"]   = 0
            state["hedge_active"] = False
            state["hedge_side"]   = None
            return jsonify({"status": "全決済完了", "result": results})

        else:
            return jsonify({"error": f"不明なアクション: {action}"}), 400

    except Exception as e:
        print(f"エラー: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status":  "稼働中",
        "message": "BB Hedge Bot v1 running!",
        "symbols": list(ORDER_SIZE.keys()),
        "order_sizes": ORDER_SIZE
    })

@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "hedge_state": hedge_state,
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
