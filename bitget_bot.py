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

TICK_SIZE = {
    "BTCUSDT": 0.1,
    "ETHUSDT": 0.01,
    "XRPUSDT": 0.0001,
}

# v6: 部分利確済みフラグ（シンボルごとに管理）
partial_closed = {
    "BTCUSDT": False,
    "ETHUSDT": False,
    "XRPUSDT": False,
}

def get_tick_size(symbol):
    return TICK_SIZE.get(symbol, 0.1)

def round_price(price, symbol):
    tick = get_tick_size(symbol)
    return round(round(price / tick) * tick, 10)

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

def get_position_detail(symbol):
    """ポジション詳細（holdSide + total）を返す"""
    path = f"/api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT"
    headers = get_headers("GET", path)
    response = requests.get(BASE_URL + path, headers=headers)
    data = response.json()
    if data.get("code") != "00000" or not data.get("data"):
        return None
    for pos in data["data"]:
        if pos.get("symbol") != symbol:
            continue
        total = float(pos.get("total", 0))
        if total > 0:
            return {
                "holdSide": pos["holdSide"],
                "total":    total,
                "available": float(pos.get("available", 0))
            }
    return None

def cancel_all_orders(symbol):
    """全ての未決済注文（SL含む）をキャンセル"""
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
        sl_rounded = round_price(stop_loss_price, symbol)
        order_body["presetStopLossPrice"] = str(sl_rounded)
        print(f"SL価格設定: {stop_loss_price} → 丸め後: {sl_rounded}")

    body = json.dumps(order_body)
    headers = get_headers("POST", path, body)
    response = requests.post(BASE_URL + path, headers=headers, data=body)
    result = response.json()
    print(f"注文結果: {result}")
    return result

def close_position(symbol):
    """
    全量決済（flash-close優先 → 失敗時place-order）
    """
    print(f"注文キャンセル開始: {symbol}")
    cancel_all_orders(symbol)
    time.sleep(0.8)

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

        total     = float(pos.get("total", 0))
        available = float(pos.get("available", 0))

        if total <= 0:
            continue

        hold_side  = pos["holdSide"]
        close_side = "sell" if hold_side == "long" else "buy"
        qty = max(total, available)
        print(f"決済数量決定: total={total}, available={available}, 使用数量={qty}")

        # flash-close API
        flash_path = "/api/v2/mix/order/close-positions"
        flash_body = json.dumps({
            "symbol":      symbol,
            "productType": "USDT-FUTURES",
            "holdSide":    hold_side
        })
        flash_headers = get_headers("POST", flash_path, flash_body)
        flash_response = requests.post(BASE_URL + flash_path, headers=flash_headers, data=flash_body)
        flash_result = flash_response.json()
        print(f"flash-close結果: {flash_result}")

        if flash_result.get("code") == "00000":
            results.append(flash_result)
            print(f"✅ flash-closeで決済成功: {symbol} {hold_side}")
            # v6: 決済完了 → 部分利確フラグリセット
            partial_closed[symbol] = False
            continue

        # フォールバック: place-order
        print(f"⚠️ flash-close失敗 → place-orderで決済試行: 数量={qty}")
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
            "force":       "gtc",
            "reduceOnly":  "YES"
        })
        headers2 = get_headers("POST", close_path, body)
        r = requests.post(BASE_URL + close_path, headers=headers2, data=body)
        result = r.json()
        results.append(result)
        print(f"決済結果: {result}")
        # v6: 決済完了 → 部分利確フラグリセット
        partial_closed[symbol] = False

    return results if results else {"message": "決済するポジションなし"}


def partial_close_position(symbol, percent=50):
    """
    v6新機能: 部分利確（指定%のポジションを決済）
    """
    print(f"部分利確開始: {symbol} {percent}%")

    # 重複防止チェック
    if partial_closed.get(symbol, False):
        print(f"⚠️ {symbol} は既に部分利確済み → スキップ")
        return {"message": "既に部分利確済み"}

    # SLキャンセル
    cancel_all_orders(symbol)
    time.sleep(0.8)

    # ポジション取得
    pos = get_position_detail(symbol)
    if not pos:
        return {"message": "ポジションなし"}

    hold_side  = pos["holdSide"]
    total      = pos["total"]
    close_side = "sell" if hold_side == "long" else "buy"

    # 決済数量 = total × percent%
    qty = round(total * (percent / 100), 4)
    print(f"部分利確数量: total={total} × {percent}% = {qty}")

    if qty <= 0:
        return {"error": "決済数量が0以下"}

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
        "force":       "gtc",
        "reduceOnly":  "YES"
    })
    headers = get_headers("POST", close_path, body)
    r = requests.post(BASE_URL + close_path, headers=headers, data=body)
    result = r.json()
    print(f"部分利確結果: {result}")

    if result.get("code") == "00000":
        partial_closed[symbol] = True
        print(f"✅ 部分利確成功: {symbol} {hold_side} {percent}% ({qty}枚)")
    else:
        print(f"❌ 部分利確失敗: {result}")

    return result


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
            partial_closed[symbol] = False  # フラグリセット
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
            partial_closed[symbol] = False  # フラグリセット
            print(f"ショート注文: {symbol} SL:{sl_price}")
            result = place_order(symbol, "sell", ORDER_SIZE_USDT, sl_price)
            return jsonify({"status": "ショート注文送信", "result": result})

        elif action == "partial_close":
            # v6: 部分利確
            percent = float(data.get("percent", 50))
            reason  = data.get("reason", "TP1")
            print(f"部分利確リクエスト: {symbol} {percent}% reason={reason}")
            result = partial_close_position(symbol, percent)
            return jsonify({"status": f"部分利確完了({percent}%)", "result": result})

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
    return jsonify({"status": "稼働中", "message": "Bitget Bot v6 is running!"})

@app.route("/price/<symbol>", methods=["GET"])
def check_price(symbol):
    price = get_current_price(symbol)
    return jsonify({"symbol": symbol, "price": price})

@app.route("/status", methods=["GET"])
def status():
    """v6: 部分利確フラグ確認用エンドポイント"""
    return jsonify({"partial_closed": partial_closed})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
