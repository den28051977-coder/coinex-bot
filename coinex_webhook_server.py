"""
CoinEx Futures Webhook Server v8
Изменения vs v7:
- Фоновый guardian поток (каждые 30 сек, не только при webhook)
- Обработка action="reverse" (атомарный разворот)
- signal сохраняется в position_state (знаем откуда был вход)
- Новые сигналы: frank_judas, whale_abs, avwap_judas, avwap_breakout
- mode поле: разные настройки риска для разных типов сигналов
- Улучшенный лог: mode, signal_type в каждой записи
"""
 
import os, hmac, hashlib, time, json, requests, threading
from flask import Flask, request, jsonify
from collections import deque
from datetime import datetime, timezone
 
app = Flask(__name__)
 
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response
 
@app.route('/signals', methods=['OPTIONS'])
def signals_options():
    return '', 204
 
# ─── Конфигурация ───
API_KEY        = os.environ.get("COINEX_API_KEY", "")
API_SECRET     = os.environ.get("COINEX_API_SECRET", "")
WEBHOOK_TOKEN  = os.environ.get("WEBHOOK_TOKEN", "mytoken123")
LEVERAGE       = int(os.environ.get("LEVERAGE", "10"))
 
DEPOSIT        = float(os.environ.get("DEPOSIT", "1000"))
LOT_PCT        = float(os.environ.get("LOT_PCT", "1.6"))
LOT_SIZE_FIXED = float(os.environ.get("LOT_SIZE", "0"))
 
MAX_LOSS_PCT     = float(os.environ.get("MAX_LOSS_PCT", "3.0"))
GUARDIAN_ENABLED = os.environ.get("GUARDIAN", "true").lower() == "true"
GUARDIAN_INTERVAL = int(os.environ.get("GUARDIAN_INTERVAL", "30"))  # секунды
 
BASE_URL = "https://api.coinex.com"
signals_log = deque(maxlen=200)
 
# ─── Состояние позиции ───
position_state = {
    "symbol":  "SOLUSDT",
    "dir":     0,       # 1=long, -1=short, 0=flat
    "avg":     0.0,
    "lots":    0,
    "avwap_tp": 0.0,
    "signal":  "",      # откуда вход: checklist / judas_asia / frank_judas / whale_abs / ...
    "power":   0,       # сила сигнала 1/2/3
    "zone":    "",      # disc_15m / prem_15m / eq_15m
}
 
# ─── Типы сигналов и их приоритет ───
SIGNAL_TYPES = {
    "checklist":       {"risk": 1.0, "label": "Чеклист"},
    "judas_asia":      {"risk": 1.0, "label": "Judas Asia"},
    "frank_judas":     {"risk": 1.0, "label": "Frank Judas"},
    "level_judas":     {"risk": 0.8, "label": "Level Judas"},
    "avwap_judas":     {"risk": 1.0, "label": "AVWAP Judas"},
    "avwap_breakout":  {"risk": 1.0, "label": "AVWAP Break"},
    "of_lf_zone":      {"risk": 1.0, "label": "OF+LF Zone"},
    "whale_abs":       {"risk": 1.2, "label": "Кит+АБС"},   # чуть крупнее лот
}
 
 
def log_signal(data, result, filled_price=None, extra=None):
    sig = data.get("signal", "")
    sig_info = SIGNAL_TYPES.get(sig, {"label": sig})
    entry = {
        "time":         datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "action":       data.get("action", ""),
        "symbol":       data.get("symbol", ""),
        "lots":         data.get("lots", 1),
        "power":        data.get("power", ""),
        "signal":       sig,
        "signal_label": sig_info.get("label", sig),
        "zone":         data.get("zone", ""),
        "trend":        data.get("trend", ""),
        "avg":          data.get("avg", ""),
        "filled_price": filled_price,
        "result":       "ok" if isinstance(result, dict) and result.get("code") == 0 else str(result.get("msg", result) if isinstance(result, dict) else result),
        "pnl":          None
    }
    if extra:
        entry.update(extra)
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
        API_SECRET.encode("utf-8"),
        sign_str.encode("utf-8"),
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
 
 
def get_current_price(symbol):
    try:
        r = api_get("/spot/ticker", {"market": symbol})
        if r.get("code") == 0:
            data = r.get("data", [])
            if isinstance(data, list) and len(data) > 0:
                return float(data[0].get("last", 0))
        r2 = api_get("/futures/ticker", {"market": symbol})
        if r2.get("code") == 0:
            data2 = r2.get("data", [])
            if isinstance(data2, list) and len(data2) > 0:
                return float(data2[0].get("last", 0))
    except Exception as e:
        print(f"  [WARN] get_current_price error: {e}")
    return 0.0
 
 
def calc_lot_size(symbol, signal=""):
    if LOT_SIZE_FIXED > 0:
        return LOT_SIZE_FIXED
    price = get_current_price(symbol)
    if price <= 0:
        print(f"  [WARN] Не удалось получить цену, fallback 0.1")
        return 0.1
    risk_mult = SIGNAL_TYPES.get(signal, {}).get("risk", 1.0)
    lot_usd   = DEPOSIT * (LOT_PCT / 100.0) * risk_mult
    lot_size  = round(lot_usd / price, 3)
    print(f"  LOT_SIZE: ${lot_usd:.2f} / {price} = {lot_size} ({signal}, risk×{risk_mult})")
    return lot_size
 
 
def get_position(symbol):
    for ep in ["/futures/pending-position", "/futures/position"]:
        r = api_get(ep, {"market": symbol, "market_type": "FUTURES"})
        print(f"  get_position [{ep}]: {json.dumps(r)[:300]}")
        if r.get("code") == 4009:
            continue
        if r.get("code") != 0:
            return None
        data = r.get("data", {})
        if isinstance(data, list):
            for p in data:
                if p.get("market") == symbol:
                    return p
            return None
        if isinstance(data, dict):
            if data.get("market") == symbol:
                return data
            for key in ["position_list", "positions"]:
                pos_list = data.get(key, [])
                if isinstance(pos_list, list):
                    for p in pos_list:
                        if p.get("market") == symbol:
                            return p
        return None
    return None
 
 
def set_leverage(symbol, leverage):
    return api_post("/futures/adjust-position-leverage", {
        "market":      symbol,
        "market_type": "FUTURES",
        "leverage":    str(leverage),
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
        print(f"  [WARN] close_position: позиция {symbol} не найдена")
        position_state["dir"]  = 0
        position_state["avg"]  = 0.0
        position_state["lots"] = 0
        return {"msg": "нет открытой позиции"}
    side   = "sell" if pos.get("side") == "long" else "buy"
    amount = str(pos.get("close_avbl", pos.get("open_interest", pos.get("amount", "0"))))
    print(f"  close_position: side={side} amount={amount}")
    result = api_post("/futures/order", {
        "market":         symbol,
        "market_type":    "FUTURES",
        "side":           side,
        "type":           "market",
        "amount":         amount,
        "close_position": True
    })
    position_state["dir"]    = 0
    position_state["avg"]    = 0.0
    position_state["lots"]   = 0
    position_state["signal"] = ""
    position_state["avwap_tp"] = 0.0
    return result
 
 
def guardian_check(symbol, source="webhook"):
    if not GUARDIAN_ENABLED or MAX_LOSS_PCT <= 0:
        return
    if position_state["dir"] == 0 or position_state["avg"] <= 0:
        return
    try:
        price = get_current_price(symbol)
        if price <= 0:
            return
        avg = position_state["avg"]
        if position_state["dir"] == 1:
            loss_pct = (avg - price) / avg * 100
        else:
            loss_pct = (price - avg) / avg * 100
 
        if loss_pct >= MAX_LOSS_PCT:
            print(f"  [GUARDIAN/{source}] Убыток {loss_pct:.2f}% >= {MAX_LOSS_PCT}% — закрываю")
            result = close_position(symbol)
            log_signal(
                {"action": "guardian_close", "symbol": symbol, "avg": str(avg), "signal": position_state.get("signal", "")},
                result,
                extra={"loss_pct": round(loss_pct, 2), "source": source}
            )
            return
 
        # AVWAP TP
        avwap_tp = position_state.get("avwap_tp", 0)
        if avwap_tp > 0:
            if position_state["dir"] == 1 and price >= avwap_tp and avg < avwap_tp:
                print(f"  [AVWAP TP/{source}] Цена {price} >= AVWAP {avwap_tp} — закрываю лонг")
                result = close_position(symbol)
                log_signal({"action": "avwap_tp", "symbol": symbol, "avg": str(avg), "signal": position_state.get("signal", "")}, result)
            elif position_state["dir"] == -1 and price <= avwap_tp and avg > avwap_tp:
                print(f"  [AVWAP TP/{source}] Цена {price} <= AVWAP {avwap_tp} — закрываю шорт")
                result = close_position(symbol)
                log_signal({"action": "avwap_tp", "symbol": symbol, "avg": str(avg), "signal": position_state.get("signal", "")}, result)
    except Exception as e:
        print(f"  [GUARDIAN ERROR] {e}")
 
 
# ─── Фоновый поток Guardian ───
def guardian_loop():
    print(f"[GUARDIAN] Фоновый поток запущен, интервал={GUARDIAN_INTERVAL}с")
    while True:
        time.sleep(GUARDIAN_INTERVAL)
        try:
            symbol = position_state.get("symbol", "SOLUSDT")
            guardian_check(symbol, source="background")
        except Exception as e:
            print(f"  [GUARDIAN LOOP ERROR] {e}")
 
if GUARDIAN_ENABLED:
    t = threading.Thread(target=guardian_loop, daemon=True)
    t.start()
 
 
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
    power  = int(data.get("power", 1))
    signal = data.get("signal", "checklist")
 
    lot_size = calc_lot_size(symbol, signal)
    amount   = round(lot_size * lots, 3)
 
    print(f"\n[{time.strftime('%H:%M:%S')}] ACTION={action} | {symbol} | lots={lots} | power={power} | signal={signal} | amount={amount} | zone={data.get('zone','')} | trend={data.get('trend','')}")
 
    # Guardian перед действием
    guardian_check(symbol, source="pre-webhook")
 
    result = {"msg": "no action"}
 
    if action == "buy":
        pos = get_position(symbol)
        if pos and pos.get("side") == "short":
            print(f"  [INFO] Открыт шорт — закрываем перед лонгом")
            close_position(symbol)
        result = place_order(symbol, "buy", amount)
        if isinstance(result, dict) and result.get("code") == 0:
            fp = result.get("data", {}).get("last_filled_price")
            avg_val = float(data.get("avg", 0) or 0)
            position_state.update({
                "dir":      1,
                "avg":      avg_val if avg_val > 0 else float(fp or 0),
                "lots":     lots,
                "symbol":   symbol,
                "signal":   signal,
                "power":    power,
                "zone":     data.get("zone", ""),
                "avwap_tp": float(data.get("avwap_mid", 0) or 0),
            })
 
    elif action == "sell":
        pos = get_position(symbol)
        if pos and pos.get("side") == "long":
            print(f"  [INFO] Открыт лонг — закрываем перед шортом")
            close_position(symbol)
        result = place_order(symbol, "sell", amount)
        if isinstance(result, dict) and result.get("code") == 0:
            fp = result.get("data", {}).get("last_filled_price")
            avg_val = float(data.get("avg", 0) or 0)
            position_state.update({
                "dir":      -1,
                "avg":      avg_val if avg_val > 0 else float(fp or 0),
                "lots":     lots,
                "symbol":   symbol,
                "signal":   signal,
                "power":    power,
                "zone":     data.get("zone", ""),
                "avwap_tp": float(data.get("avwap_mid", 0) or 0),
            })
 
    elif action == "close_all":
        result = close_position(symbol)
 
    elif action == "reverse":
        # Атомарный разворот — закрыть старую и открыть новую
        print(f"  [REVERSE] Закрываем текущую позицию и открываем в другую сторону")
        close_position(symbol)
        # Направление определяем по полю trend или lots_closed
        new_side = data.get("new_side", "")
        if new_side in ("buy", "sell"):
            result = place_order(symbol, new_side, amount)
            if isinstance(result, dict) and result.get("code") == 0:
                fp = result.get("data", {}).get("last_filled_price")
                avg_val = float(data.get("avg", 0) or 0)
                position_state.update({
                    "dir":      1 if new_side == "buy" else -1,
                    "avg":      avg_val if avg_val > 0 else float(fp or 0),
                    "lots":     lots,
                    "symbol":   symbol,
                    "signal":   signal,
                    "power":    power,
                    "zone":     data.get("zone", ""),
                    "avwap_tp": float(data.get("avwap_mid", 0) or 0),
                })
        else:
            result = {"msg": "reverse: нет new_side"}
 
    elif action == "unload":
        pos = get_position(symbol)
        if pos:
            side   = "sell" if pos["side"] == "long" else "buy"
            result = place_order(symbol, side, lot_size)
            if isinstance(result, dict) and result.get("code") == 0:
                position_state["lots"] = max(0, position_state["lots"] - 1)
        else:
            result = {"msg": "нет позиции для выгрузки"}
 
    elif action in ("trend", "guardian_close", "avwap_tp"):
        # Информационные события — только логируем
        print(f"  [INFO] Событие {action} — только лог")
        result = {"msg": f"logged: {action}"}
 
    else:
        return jsonify({"error": f"unknown action: {action}"}), 400
 
    print(f"  ИТОГ: {result}")
    log_signal(data, result)
    return jsonify({"ok": True, "result": result})
 
 
@app.route("/", methods=["GET"])
def health():
    pos = get_position("SOLUSDT")
    return jsonify({
        "status":   "ok",
        "server":   "CoinEx Webhook v8",
        "position": pos,
        "state":    position_state,
        "config": {
            "deposit":          DEPOSIT,
            "lot_pct":          LOT_PCT,
            "leverage":         LEVERAGE,
            "lot_fixed":        LOT_SIZE_FIXED,
            "max_loss_pct":     MAX_LOSS_PCT,
            "guardian":         GUARDIAN_ENABLED,
            "guardian_interval": GUARDIAN_INTERVAL,
        }
    })
 
 
@app.route("/position/<symbol>", methods=["GET"])
def check_position(symbol):
    pos = get_position(symbol.upper())
    return jsonify({"position": pos, "state": position_state})
 
 
@app.route("/signals", methods=["GET"])
def get_signals():
    limit   = int(request.args.get("limit", 50))
    signals = list(signals_log)[-limit:]
    signals.reverse()
    return jsonify({"count": len(signals), "signals": signals})
 
 
@app.route("/guardian", methods=["GET"])
def guardian_status():
    symbol = request.args.get("symbol", "SOLUSDT").upper()
    guardian_check(symbol, source="manual")
    return jsonify({
        "state":        position_state,
        "guardian":     GUARDIAN_ENABLED,
        "max_loss_pct": MAX_LOSS_PCT,
        "interval_sec": GUARDIAN_INTERVAL,
    })
 
 
@app.route("/close", methods=["POST"])
def manual_close():
    """Ручное закрытие позиции через POST /close?token=xxx"""
    if request.args.get("token", "") != WEBHOOK_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    symbol = request.args.get("symbol", "SOLUSDT").upper()
    result = close_position(symbol)
    log_signal({"action": "manual_close", "symbol": symbol, "signal": "manual"}, result)
    return jsonify({"ok": True, "result": result})
 
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Сервер запущен на порту {port}")
    print(f"DEPOSIT={DEPOSIT}, LOT_PCT={LOT_PCT}%, LEVERAGE={LEVERAGE}x")
    print(f"GUARDIAN={GUARDIAN_ENABLED}, MAX_LOSS={MAX_LOSS_PCT}%, INTERVAL={GUARDIAN_INTERVAL}s")
    app.run(host="0.0.0.0", port=port)
 
import os, hmac, hashlib, time, json, requests
from flask import Flask, request, jsonify
from collections import deque
from datetime import datetime, timezone
 
app = Flask(__name__)
 
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response
 
@app.route('/signals', methods=['OPTIONS'])
def signals_options():
    return '', 204
 
# ─── Конфигурация ───
API_KEY        = os.environ.get("COINEX_API_KEY", "")
API_SECRET     = os.environ.get("COINEX_API_SECRET", "")
WEBHOOK_TOKEN  = os.environ.get("WEBHOOK_TOKEN", "mytoken123")
LEVERAGE       = int(os.environ.get("LEVERAGE", "10"))
 
# Размер лота — динамический (% от депозита)
DEPOSIT        = float(os.environ.get("DEPOSIT", "1000"))
LOT_PCT        = float(os.environ.get("LOT_PCT", "1.6"))       # % от депозита на 1 лот
LOT_SIZE_FIXED = float(os.environ.get("LOT_SIZE", "0"))        # 0 = авто, >0 = фиксированный
 
# Серверный стоп — защита от зависших позиций
MAX_LOSS_PCT   = float(os.environ.get("MAX_LOSS_PCT", "3.0"))  # % от средней цены
GUARDIAN_ENABLED = os.environ.get("GUARDIAN", "true").lower() == "true"
 
BASE_URL = "https://api.coinex.com"
signals_log = deque(maxlen=200)
 
# ─── Текущее состояние позиции (для guardian) ───
position_state = {
    "symbol": "SOLUSDT",
    "dir": 0,        # 1=long, -1=short, 0=flat
    "avg": 0.0,
    "lots": 0,
    "avwap_tp": 0.0, # тейк на AVWAP mid (0 = не установлен)
}
 
 
def log_signal(data, result, filled_price=None):
    entry = {
        "time":         datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "action":       data.get("action", ""),
        "symbol":       data.get("symbol", ""),
        "lots":         data.get("lots", 1),
        "power":        data.get("power", ""),
        "signal":       data.get("signal", ""),
        "zone":         data.get("zone", ""),
        "trend":        data.get("trend", ""),
        "avg":          data.get("avg", ""),
        "filled_price": filled_price,
        "result":       "ok" if isinstance(result, dict) and result.get("code") == 0 else str(result.get("msg", result)),
        "pnl":          None
    }
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
        API_SECRET.encode("utf-8"),
        sign_str.encode("utf-8"),
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
 
 
def get_current_price(symbol):
    """Получить текущую цену для расчёта LOT_SIZE."""
    try:
        r = api_get("/spot/ticker", {"market": symbol})
        if r.get("code") == 0:
            data = r.get("data", [])
            if isinstance(data, list) and len(data) > 0:
                return float(data[0].get("last", 0))
        # Fallback — фьючерс тикер
        r2 = api_get("/futures/ticker", {"market": symbol})
        if r2.get("code") == 0:
            data2 = r2.get("data", [])
            if isinstance(data2, list) and len(data2) > 0:
                return float(data2[0].get("last", 0))
    except Exception as e:
        print(f"  [WARN] get_current_price error: {e}")
    return 0.0
 
 
def calc_lot_size(symbol):
    """Динамический размер лота: deposit × lot_pct% / price."""
    if LOT_SIZE_FIXED > 0:
        return LOT_SIZE_FIXED
    price = get_current_price(symbol)
    if price <= 0:
        print(f"  [WARN] Не удалось получить цену, используем fallback 0.1")
        return 0.1
    lot_usd = DEPOSIT * (LOT_PCT / 100.0)
    lot_size = round(lot_usd / price, 3)
    print(f"  LOT_SIZE авто: ${lot_usd:.2f} / {price} = {lot_size} {symbol[:3]}")
    return lot_size
 
 
def get_position(symbol):
    # CoinEx v2 — пробуем актуальные эндпоинты по очереди
    for ep in ["/futures/pending-position", "/futures/position"]:
        r = api_get(ep, {"market": symbol, "market_type": "FUTURES"})
        print(f"  get_position [{ep}]: {json.dumps(r)[:300]}")
        if r.get("code") == 4009:
            continue  # unknown method — следующий
        if r.get("code") != 0:
            return None
        data = r.get("data", {})
        if isinstance(data, list):
            for p in data:
                if p.get("market") == symbol:
                    return p
            return None  # пустой список = нет позиции
        if isinstance(data, dict):
            if data.get("market") == symbol:
                return data
            for key in ["position_list", "positions"]:
                pos_list = data.get(key, [])
                if isinstance(pos_list, list):
                    for p in pos_list:
                        if p.get("market") == symbol:
                            return p
        return None
    return None
 
 
def set_leverage(symbol, leverage):
    return api_post("/futures/adjust-position-leverage", {
        "market":      symbol,
        "market_type": "FUTURES",
        "leverage":    str(leverage),
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
        print(f"  [WARN] close_position: позиция {symbol} не найдена")
        return {"msg": "нет открытой позиции"}
    side   = "sell" if pos.get("side") == "long" else "buy"
    amount = str(pos.get("close_avbl", pos.get("open_interest", pos.get("amount", "0"))))
    print(f"  close_position: side={side} amount={amount}")
    result = api_post("/futures/order", {
        "market":         symbol,
        "market_type":    "FUTURES",
        "side":           side,
        "type":           "market",
        "amount":         amount,
        "close_position": True
    })
    # Сбрасываем состояние
    position_state["dir"]  = 0
    position_state["avg"]  = 0.0
    position_state["lots"] = 0
    return result
 
 
def guardian_check(symbol):
    """
    Серверный стоп — проверяет убыток без алерта из TV.
    Вызывается периодически или при каждом webhook запросе.
    """
    if not GUARDIAN_ENABLED or MAX_LOSS_PCT <= 0:
        return
    if position_state["dir"] == 0 or position_state["avg"] <= 0:
        return
    price = get_current_price(symbol)
    if price <= 0:
        return
    avg = position_state["avg"]
    if position_state["dir"] == 1:
        loss_pct = (avg - price) / avg * 100
    else:
        loss_pct = (price - avg) / avg * 100
    if loss_pct >= MAX_LOSS_PCT:
        print(f"  [GUARDIAN] Убыток {loss_pct:.2f}% >= {MAX_LOSS_PCT}% — закрываю позицию")
        result = close_position(symbol)
        log_signal({"action": "guardian_close", "symbol": symbol, "avg": str(avg)}, result)
        return
 
    # AVWAP TP — серверный тейк если цена достигла AVWAP mid
    avwap_tp = position_state.get("avwap_tp", 0)
    if avwap_tp > 0:
        if position_state["dir"] == 1 and price >= avwap_tp and avg < avwap_tp:
            print(f"  [AVWAP TP] Цена {price} >= AVWAP {avwap_tp} — закрываю лонг")
            result = close_position(symbol)
            log_signal({"action": "avwap_tp", "symbol": symbol, "avg": str(avg)}, result)
        elif position_state["dir"] == -1 and price <= avwap_tp and avg > avwap_tp:
            print(f"  [AVWAP TP] Цена {price} <= AVWAP {avwap_tp} — закрываю шорт")
            result = close_position(symbol)
            log_signal({"action": "avwap_tp", "symbol": symbol, "avg": str(avg)}, result)
 
 
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
    power  = int(data.get("power", 1))   # сила сигнала из Pine 1/2/3
 
    # Динамический размер одного лота
    lot_size = calc_lot_size(symbol)
    amount   = round(lot_size * lots, 3)
 
    print(f"\n[{time.strftime('%H:%M:%S')}] ACTION={action} | {symbol} | lots={lots} | power={power} | amount={amount} | zone={data.get('zone','')} | trend={data.get('trend','')}")
 
    # Guardian перед любым действием
    guardian_check(symbol)
 
    if action == "buy":
        # Если открыт шорт — сначала закрываем
        pos = get_position(symbol)
        if pos and pos.get("side") == "short":
            print(f"  [INFO] Открыт шорт — закрываем перед лонгом")
            close_position(symbol)
        result = place_order(symbol, "buy", amount)
        if isinstance(result, dict) and result.get("code") == 0:
            fp = result.get("data", {}).get("last_filled_price")
            if fp:
                avg_val = float(data.get("avg", 0) or 0)
                position_state["dir"]    = 1
                position_state["avg"]    = avg_val if avg_val > 0 else float(fp)
                position_state["lots"]   = lots
                position_state["symbol"] = symbol
                avwap_val = float(data.get("avwap_mid", 0) or 0)
                position_state["avwap_tp"] = avwap_val
 
    elif action == "sell":
        # Если открыт лонг — сначала закрываем
        pos = get_position(symbol)
        if pos and pos.get("side") == "long":
            print(f"  [INFO] Открыт лонг — закрываем перед шортом")
            close_position(symbol)
        result = place_order(symbol, "sell", amount)
        if isinstance(result, dict) and result.get("code") == 0:
            fp = result.get("data", {}).get("last_filled_price")
            if fp:
                avg_val = float(data.get("avg", 0) or 0)
                position_state["dir"]    = -1
                position_state["avg"]    = avg_val if avg_val > 0 else float(fp)
                position_state["lots"]   = lots
                position_state["symbol"] = symbol
                avwap_val = float(data.get("avwap_mid", 0) or 0)
                position_state["avwap_tp"] = avwap_val
 
    elif action == "close_all":
        result = close_position(symbol)
 
    elif action == "unload":
        pos = get_position(symbol)
        if pos:
            side   = "sell" if pos["side"] == "long" else "buy"
            result = place_order(symbol, side, lot_size)
        else:
            result = {"msg": "нет позиции для выгрузки"}
 
    else:
        return jsonify({"error": f"unknown action: {action}"}), 400
 
    print(f"  ИТОГ: {result}")
    log_signal(data, result)
    return jsonify({"ok": True, "result": result})
 
 
@app.route("/", methods=["GET"])
def health():
    pos = get_position("SOLUSDT")
    return jsonify({
        "status":   "ok",
        "server":   "CoinEx Webhook v7",
        "position": pos,
        "state":    position_state,
        "config": {
            "deposit":     DEPOSIT,
            "lot_pct":     LOT_PCT,
            "leverage":    LEVERAGE,
            "lot_fixed":   LOT_SIZE_FIXED,
            "max_loss_pct": MAX_LOSS_PCT,
            "guardian":    GUARDIAN_ENABLED,
        }
    })
 
 
@app.route("/position/<symbol>", methods=["GET"])
def check_position(symbol):
    pos = get_position(symbol.upper())
    return jsonify({"position": pos})
 
 
@app.route("/signals", methods=["GET"])
def get_signals():
    limit   = int(request.args.get("limit", 50))
    signals = list(signals_log)[-limit:]
    signals.reverse()
    return jsonify({"count": len(signals), "signals": signals})
 
 
@app.route("/guardian", methods=["GET"])
def guardian_status():
    """Ручная проверка guardian."""
    symbol = request.args.get("symbol", "SOLUSDT").upper()
    guardian_check(symbol)
    return jsonify({"state": position_state, "guardian": GUARDIAN_ENABLED, "max_loss_pct": MAX_LOSS_PCT})
 
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Сервер запущен на порту {port}")
    print(f"DEPOSIT={DEPOSIT}, LOT_PCT={LOT_PCT}%, LEVERAGE={LEVERAGE}x")
    print(f"GUARDIAN={GUARDIAN_ENABLED}, MAX_LOSS={MAX_LOSS_PCT}%")
    app.run(host="0.0.0.0", port=port)
