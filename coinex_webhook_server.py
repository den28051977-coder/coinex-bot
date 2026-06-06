"""
CoinEx Futures Webhook Server v5 — с журналом сигналов
"""

import os, hmac, hashlib, time, json, requests
from flask import Flask, request, jsonify
from collections import deque
from datetime import datetime, timezone

app = Flask(__name__)

# CORS — разрешаем запросы отовсюду для /signals
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

@app.route('/signals', methods=['OPTIONS'])
def signals_options():
    return '', 204

API_KEY       = os.environ.get("COINEX_API_KEY", "")
API_SECRET    = os.environ.get("COINEX_API_SECRET", "")
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "mytoken123")
LOT_SIZE      = float(os.environ.get("LOT_SIZE", "0.1"))
LEVERAGE      = int(os.environ.get("LEVERAGE", "10"))

BASE_URL = "https://api.coinex.com"

# === ЖУРНАЛ СИГНАЛОВ — хранит последние 200 сигналов ===
signals_log = deque(maxlen=200)

def log_signal(data, result, filled_price=None):
    """Записываем каждый сигнал в журнал"""
    entry = {
        "time":        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "action":      data.get("action", ""),
        "symbol":      data.get("symbol", ""),
        "lots":        data.get("lots", 1),
        "signal":      data.get("signal", ""),
        "trend":       data.get("trend", ""),
        "mode":        data.get("mode", ""),
        "avg":         data.get("avg", ""),
        "filled_price": filled_price,
        "result":      "ok" if isinstance(result, dict) and result.get("code") == 0 else str(result.get("msg", result)),
        "pnl":         None
    }
    # Извлекаем PNL из результата биржи
    if isinstance(result, dict) and result.get("code") == 0:
        pnl = result.get("data", {}).get("realized_pnl")
        if pnl:
            entry["pnl"] = float(pnl)
        fp = result.get("data", {}).get("last_filled_price")
        if fp:
            entry["filled_price"] = float(fp)
    signals_log.append(entry)

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
    r = api_get("/futures/pending-position", {"market": symbol, "market_type": "FUTURES"})
    print(f"  get_position raw: {json.dumps(r)[:400]}")
    if r.get("code") == 0:
        data = r.get("data", {})
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
    # Всегда открываем один лот = LOT_SIZE, независимо от lots
    # lots используется только для информации в логах
    amount = LOT_SIZE  # фиксированный размер одного добора

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

    # Логируем сигнал в журнал
    log_signal(data, result)

    return jsonify({"ok": True, "result": result})

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "server": "CoinEx Webhook v5"})

@app.route("/position/<symbol>", methods=["GET"])
def check_position(symbol):
    pos = get_position(symbol.upper())
    return jsonify({"position": pos})

# === НОВЫЙ ЭНДПОИНТ — журнал сигналов ===
@app.route("/signals", methods=["GET"])
def get_signals():
    """Отдаёт историю сигналов. Доступно без токена для дашборда."""
    limit = int(request.args.get("limit", 50))
    signals = list(signals_log)[-limit:]
    signals.reverse()  # Новые сверху
    return jsonify({
        "count": len(signals),
        "signals": signals
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Сервер запущен на порту {port}")
    app.run(host="0.0.0.0", port=port)
