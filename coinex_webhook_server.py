"""
CoinEx Futures Webhook Server v4 — с детальным логированием
"""

import os, hmac, hashlib, time, json, requests
from flask import Flask, request, jsonify

app = Flask(__name__)

API_KEY       = os.environ.get("COINEX_API_KEY", "")
API_SECRET    = os.environ.get("COINEX_API_SECRET", "")
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "mytoken123")
LOT_SIZE      = float(os.environ.get("LOT_SIZE", "0.1"))
LEVERAGE      = int(os.environ.get("LEVERAGE", "10"))

BASE_URL = "https://api.coinex.com"

def sign_request(method, path, body=""):
    timestamp = str(int(time.time() * 1000))
    sign_str  = method.upper() + path + body + timestamp
    signature = hmac.new(
        API_SECRET.encode("latin-1"),
        sign_str.encode("latin-1"),
        hashlib.sha256
    ).hexdigest()
    return {
        "X-COINEX-KEY":       API_KEY,
        "X-COINEX-SIGN":      signature,
        "X-COINEX-TIMESTAMP": timestamp,
        "Content-Type":       "application/json",
    }

def api_post(path, payload):
    full_path = "/v2" + path
    body      = json.dumps(payload, separators=(",", ":"))
    headers   = sign_request("POST", full_path, body)
    r = requests.post(BASE_URL + full_path, headers=headers, data=body, timeout=10)
    print(f"  POST {full_path} → {r.status_code}: {r.text[:300]}")
    return r.json()

def api_get(path, params=None):
    import urllib.parse
    query     = urllib.parse.urlencode(params or {})
    full_path = "/v2" + path + ("?" + query if query else "")
    headers   = sign_request("GET", full_path, "")
    r = requests.get(BASE_URL + full_path, headers=headers, timeout=10)
    print(f"  GET {full_path} → {r.status_code}: {r.text[:300]}")
    return r.json()

def get_position(symbol):
    # Пробуем оба варианта endpoint
    r = api_get("/futures/pending-position", {"market": symbol, "market_type": "FUTURES"})
    print(f"  get_position raw: {json.dumps(r)[:400]}")
    if r.get("code") == 0:
        data = r.get("data", {})
        # Проверяем разные форматы ответа
        if isinstance(data, list):
            for p in data:
                if p.get("market") == symbol:
                    return p
        elif isinstance(data, dict):
            pos_list = data.get("position_list", data.get("positions", []))
            for p in pos_list:
                if p.get("market") == symbol:
                    return p
    return None

def set_leverage(symbol, leverage):
    return api_post("/futures/adjust-position-leverage", {
        "market":        symbol,
        "market_type":   "FUTURES",
        "leverage":      str(leverage),
        "position_side": "both"
    })

def place_order(symbol, side, amount):
    set_leverage(symbol, LEVERAGE)
    return api_post("/futures/order", {
        "market":      symbol,
        "market_type": "FUTURES",
        "side":        side,
        "type":        "market",
        "amount":      str(amount),
    })

def close_position(symbol):
    pos = get_position(symbol)
    if not pos:
        return {"msg": "нет открытой позиции"}
    side = "sell" if pos["side"] == "long" else "buy"
    return api_post("/futures/order", {
        "market":         symbol,
        "market_type":    "FUTURES",
        "side":           side,
        "type":           "market",
        "amount":         str(pos.get("close_avbl", pos.get("open_interest", "0"))),
        "close_position": True
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    if request.args.get("token", "") != WEBHOOK_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "no json"}), 400

    action = data.get("action", "").lower()
    symbol = data.get("symbol", "SOLUSDT").upper()
    lots   = int(data.get("lots", 1))
    amount = round(LOT_SIZE * lots, 4)

    print(f"\n[{time.strftime('%H:%M:%S')}] ACTION={action} | {symbol} | lots={lots} | amount={amount}")

    if action == "buy":
        result = place_order(symbol, "buy", amount)
    elif action == "sell":
        result = place_order(symbol, "sell", amount)
    elif action == "close_all":
        result = close_position(symbol)
    elif action == "unload":
        pos = get_position(symbol)
        if pos:
            side = "sell" if pos["side"] == "long" else "buy"
            result = place_order(symbol, side, amount)
        else:
            result = {"msg": "нет позиции для выгрузки"}
    else:
        return jsonify({"error": f"unknown action: {action}"}), 400

    print(f"  ИТОГ: {result}")
    return jsonify({"ok": True, "result": result})

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "server": "CoinEx Webhook v4"})

# Тестовый endpoint для проверки позиции
@app.route("/position/<symbol>", methods=["GET"])
def check_position(symbol):
    pos = get_position(symbol.upper())
    return jsonify({"position": pos})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Сервер запущен на порту {port}")
    app.run(host="0.0.0.0", port=port)
