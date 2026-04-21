"""
TradingView Webhook → Bitget API 自動売買サーバー
================================================
使い方：
1. このファイルを render.com や railway.app に無料デプロイ
2. TradingViewのアラートのWebhook URLに 「https://あなたのURL/webhook」を設定
3. アラートメッセージに下記のJSONを設定する

【TradingViewアラートメッセージ設定例】
ロング：{"action": "long",  "symbol": "BTCUSDT", "secret": "mySecretKey123"}
ショート：{"action": "short", "symbol": "BTCUSDT", "secret": "mySecretKey123"}
決済：{"action": "close", "symbol": "BTCUSDT", "secret": "mySecretKey123"}
"""

from flask import Flask, request, jsonify
import hmac
import hashlib
import time
import requests
import json
import os

app = Flask(__name__)

# ============================================================
# 設定（環境変数で管理するのが安全）
# ============================================================
BITGET_API_KEY    = os.environ.get("BITGET_API_KEY", "ここにAPIキーを入力")
BITGET_SECRET_KEY = os.environ.get("BITGET_SECRET_KEY", "ここにシークレットキーを入力")
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "ここにパスフレーズを入力")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "bitget_master_bot")  # TradingViewと同じ文字列

# 注文設定
ORDER_SIZE_USDT = 10  # 1注文あたりのUSDT（テスト用：10ドル≒約1500円）
LEVERAGE        = 1   # レバレッジ（テスト中は1倍推奨）

# BitgetのAPIエンドポイント
BASE_URL = "https://api.bitget.com"

# ============================================================
# Bitget API署名生成
# ============================================================
def generate_signature(timestamp, method, request_path, body=""):
    message = str(timestamp) + method.upper() + request_path + body
    signature = hmac.new(
        BITGET_SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).digest()
    import base64
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

# ============================================================
# Bitget：現在価格取得
# ============================================================
def get_current_price(symbol):
    url = f"{BASE_URL}/api/v2/mix/market/ticker?symbol={symbol}&productType=USDT-FUTURES"
    response = requests.get(url)
    data = response.json()
    if data.get("code") == "00000":
        return float(data["data"][0]["lastPr"])
    return None

# ============================================================
# Bitget：レバレッジ設定
# ============================================================
def set_leverage(symbol, leverage):
    path = "/api/v2/mix/account/set-leverage"
    body = json.dumps({
        "symbol":      symbol,
        "productType": "USDT-FUTURES",
        "marginCoin":  "USDT",
        "leverage":    str(leverage),
        "holdSide":    "long"
    })
    headers = get_headers("POST", path, body)
    requests.post(BASE_URL + path, headers=headers, data=body)

    body2 = json.dumps({
        "symbol":      symbol,
        "productType": "USDT-FUTURES",
        "marginCoin":  "USDT",
        "leverage":    str(leverage),
        "holdSide":    "short"
    })
    headers2 = get_headers("POST", path, body2)
    requests.post(BASE_URL + path, headers=headers2, data=body2)

# ============================================================
# Bitget：注文送信
# ============================================================
def place_order(symbol, side, size_usdt):
    price = get_current_price(symbol)
    if not price:
        return {"error": "価格取得失敗"}

    # 注文数量計算（USDT ÷ 価格）
    qty = round(size_usdt / price, 4)

    path = "/api/v2/mix/order/place-order"
    body = json.dumps({
        "symbol":      symbol,
        "productType": "USDT-FUTURES",
        "marginMode":  "isolated",
        "marginCoin":  "USDT",
        "size":        str(qty),
        "side":        side,        # "buy" or "sell"
        "tradeSide":   "open",
        "orderType":   "market",
        "force":       "gtc"
    })
    headers = get_headers("POST", path, body)
    response = requests.post(BASE_URL + path, headers=headers, data=body)
    return response.json()

# ============================================================
# Bitget：ポジション決済
# ============================================================
def close_position(symbol):
    # 現在のポジション確認
    path = f"/api/v2/mix/position/single-position?symbol={symbol}&productType=USDT-FUTURES&marginCoin=USDT"
    headers = get_headers("GET", path)
    response = requests.get(BASE_URL + path, headers=headers)
    data = response.json()

    if data.get("code") != "00000" or not data.get("data"):
        return {"message": "ポジションなし"}

    results = []
    for pos in data["data"]:
        if float(pos.get("available", 0)) > 0:
            hold_side = pos["holdSide"]  # "long" or "short"
            close_side = "sell" if hold_side == "long" else "buy"
            qty = pos["available"]

            close_path = "/api/v2/mix/order/place-order"
            body = json.dumps({
                "symbol":      symbol,
                "productType": "USDT-FUTURES",
                "marginMode":  "isolated",
                "marginCoin":  "USDT",
                "size":        str(qty),
                "side":        close_side,
                "tradeSide":   "close",
                "orderType":   "market",
                "force":       "gtc"
            })
            headers2 = get_headers("POST", close_path, body)
            r = requests.post(BASE_URL + close_path, headers=headers2, data=body)
            results.append(r.json())

    return results if results else {"message": "決済するポジションなし"}

# ============================================================
# Webhookエンドポイント（TradingViewから受信）
# ============================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        if request.is_json:
            data = request.get_json()
        else:
            data = json.loads(request.data.decode('utf-8'))
        
        print(f"受信データ: {data}")

        # ① シークレットキー認証
        if data.get("secret") != WEBHOOK_SECRET:
            print("認証失敗")
            return jsonify({"error": "認証失敗"}), 403

        action = data.get("action", "").lower()
        symbol = data.get("symbol", "BTCUSDT")

        # ② レバレッジ設定
        set_leverage(symbol, LEVERAGE)

        # ③ アクションに応じて注文
        if action == "long":
            print(f"ロング注文: {symbol}")
            result = place_order(symbol, "buy", ORDER_SIZE_USDT)
            print(f"注文結果: {result}")
            return jsonify({"status": "ロング注文送信", "result": result})

        elif action == "short":
            print(f"ショート注文: {symbol}")
            result = place_order(symbol, "sell", ORDER_SIZE_USDT)
            print(f"注文結果: {result}")
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

# ============================================================
# 動作確認用エンドポイント
# ============================================================
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
