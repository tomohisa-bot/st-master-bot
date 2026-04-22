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

ORDER_SIZE_USDT = 10
LEVERAGE        = 1
BASE_URL = "https://api.bitget.com"

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

def round_price(price):
    """Bitgetの価格単位（0.1）に丸める"""
    return round(round(price * 10) / 10, 1)

def place_order(symbol, side, size_usdt, stop_loss_price=None):
    price = get_current_price(symbol)
    if not price:
        return {"error": "価格取得失敗"}
    qty = round(size_usdt / price, 4)
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
    
    if stop_loss_price:
        sl_rounded = round_price(stop_loss_price)
        order_body["presetStopLossPrice"] = str(sl_rounded)
        print(f"SL価格設定: {stop_loss_price} → 丸め後: {sl_rounded}")
    
    body = json.dumps(order_body)
    headers = get_headers("POST", path, body)
    response = requests.post(BASE_URL + path, headers=headers, data=body)
    result = response.json()
    print(f"注文結果: {result}")
    return result

def close_position(symbol):
    path = f"/api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT"
    headers = get_headers("GET", path)
    response = requests.get(BASE_URL + path, headers=headers)
    data = response.json()
    print(f"全ポジション取得結果: {data}")

    if data.get("code") != "00000" or not data.get("data"):
        return {"message": "ポジションなし"}

    results = []
    for pos in data["data"]:
        if pos.get("symbol") != symbol:
            continue
        total = float(pos.get("total", 0))
        available = float(pos.get("available", 0))
        if total <= 0:
            continue
        hold_side = pos["holdSide"]
        close_side = "sell" if hold_side == "long" else "buy"
        qty = str(available) if available > 0 else str(total)
        print(f"決済実行: {symbol} {hold_side} → {close_side} 数量:{qty}")
        close_path = "/api/v2/mix/order/place-order"
        body = json.dumps({
            "symbol":      symbol,
            "productType": "USDT-FUTURES",
            "marginMode":  "isolated",
            "marginCoin":  "USDT",
            "size":        qty,
            "side":        close_side,
            "tradeSide":   "close",
            "orderType":   "market",
            "force":       "gtc"
        })
        headers2 = get_headers("POST", close_path, body)
        r = requests.post(BASE_URL + close_path, headers=headers2, data=body)
        results.append(r.json())
        print(f"決済結果: {r.json()}")

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

        action = data.get("action", "").lower()
        symbol = data.get("symbol", "BTCUSDT")
        
        sl_price = data.get("sl_price", None)
        if sl_price:
            sl_price = float(sl_price)
            print(f"SL価格受信: {sl_price}")

        set_leverage(symbol, LEVERAGE)

        current_pos = get_current_position(symbol)
        print(f"現在のポジション: {current_pos}")

        if action == "long":
            if current_pos == "long":
                print("既にロングポジションあり → スキップ")
                return jsonify({"status": "スキップ（既にロング）"})
            if current_pos == "short":
                print("ショートポジションを先に決済")
                close_position(symbol)
                time.sleep(1)
            print(f"ロング注文: {symbol} SL:{sl_price}")
            result = place_order(symbol, "buy", ORDER_SIZE_USDT, sl_price)
            return jsonify({"status": "ロング注文送信", "result": result})

        elif action == "short":
            if current_pos == "short":
                print("既にショートポジションあり → スキップ")
                return jsonify({"status": "スキップ（既にショート）"})
            if current_pos == "long":
                print("ロングポジションを先に決済")
                close_position(symbol)
                time.sleep(1)
            print(f"ショート注文: {symbol} SL:{sl_price}")
            result = place_order(symbol, "sell", ORDER_SIZE_USDT, sl_price)
            return jsonify({"status": "ショート注文送信", "result": result})

        elif action == "close":
            print(f"決済: {symbol}")
            result = close_position(symbol)
            print(f"決済結果: {result}")
            return jsonify({"status": "決済完了", "result": result})

        else:
            return jsonify({"error": f"不明なアクション: {action}"}), 400

    except Exception as e:
        print(f"エラー: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "稼働中", "message": "Bitget Bot is running!"})

@app.route("/price/<symbol>", methods=["GET"])
def check_price(symbol):
    price = get_current_price(symbol)
    return jsonify({"symbol": symbol, "price": price})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
