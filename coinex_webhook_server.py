"""
CoinEx Futures Webhook Server v8
Изменения vs v7:
- Фоновый guardian поток каждые 30 сек (не только при webhook)
- ensure_leverage — леверидж устанавливается один раз, не при каждом ордере
- signal сохраняется в position_state
- Новые сигналы: frank_judas, whale_abs, avwap_judas, avwap_breakout
- risk множитель по типу сигнала (whale_abs x1.2, level_judas x0.8)
- action=reverse атомарный разворот
- POST /close ручное закрытие
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
API_KEY           = os.environ.get("COINEX_API_KEY", "")
API_SECRET        = os.environ.get("COINEX_API_SECRET", "")
WEBHOOK_TOKEN     = os.environ.get("WEBHOOK_TOKEN", "mytoken123")
LEVERAGE          = int(os.environ.get("LEVERAGE", "10"))
DEPOSIT           = float(os.environ.get("DEPOSIT", "1000"))
LOT_PCT           = float(os.environ.get("LOT_PCT", "1.6"))
LOT_SIZE_FIXED    = float(os.environ.get("LOT_SIZE", "0"))
MAX_LOSS_PCT      = float(os.environ.get("MAX_LOSS_PCT", "3.0"))
GUARDIAN_ENABLED  = os.environ.get("GUARDIAN", "true").lower() == "true"
GUARDIAN_INTERVAL = int(os.environ.get("GUARDIAN_INTERVAL", "30"))
 
BASE_URL    = "https://api.coinex.com"
signals_log = deque(maxlen=200)
 
# ─── Состояние позиции ───
position_state = {
    "symbol":   "SOLUSDT",
    "dir":      0,
    "avg":      0.0,
    "lots":     0,
    "avwap_tp": 0.0,
    "signal":   "",
    "power":    0,
    "zone":     "",
}
 
# ─── Типы сигналов и risk множитель ───
SIGNAL_TYPES = {
    "checklist":      {"risk": 1.0, "label": "Чеклист"},
    "judas_asia":     {"risk": 1.0, "label": "Judas Asia"},
    "frank_judas":    {"risk": 1.0, "label": "Frank Judas"},
    "level_judas":    {"risk": 0.8, "label": "Level Judas"},
    "avwap_judas":    {"risk": 1.0, "label": "AVWAP Judas"},
    "avwap_breakout": {"risk": 1.0, "label": "AVWAP Break"},
    "of_lf_zone":     {"risk": 1.0, "label": "OF+LF Zone"},
    "whale_abs":      {"risk": 1.2, "label": "Кит+АБС"},
}
 
 
def log_signal(data, result, extra=None):
    sig      = data.get("signal", "")
    sig_info = SIGNAL_TYPES.get(sig, {"label": sig})
    code     = result.get("code") if isinstance(result, dict) else None
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
        "filled_price": None,
        "pnl":          None,
        "result":       "ok" if code == 0 else str(result.get("message", result) if isinstance(result, dict) else result),
    }
    if extra:
        entry.update(extra)
    if isinstance(result, dict) and code == 0:
        d = result.get("data", {})
        if d.get("realized_pnl"):
            entry["pnl"] = float(d["realized_pnl"])
        if d.get("last_filled_price"):
            entry["filled_price"] = float(d["last_filled_price"])
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
    print(f"  POST {full_path} -> {r.status_code}: {r.text[:300]}")
    return r.json()
 
 
def api_get(path, params=None):
    import urllib.parse
    query     = urllib.parse.urlencode(params or {})
    full_path = "/v2" + path + ("?" + query if query else "")
    headers   = sign_request("GET", full_path, "")
    r = requests.get(BASE_URL + full_path, headers=headers, timeout=10)
    print(f"  GET {full_path} -> {r.status_code}: {r.text[:300]}")
    return r.json()
 
 
def get_current_price(symbol):
    try:
        r = api_get("/futures/ticker", {"market": symbol})
        if r.get("code") == 0:
            data = r.get("data", [])
            if isinstance(data, list) and data:
                return float(data[0].get("last", 0))
        r2 = api_get("/spot/ticker", {"market": symbol})
        if r2.get("code") == 0:
            data2 = r2.get("data", [])
            if isinstance(data2, list) and data2:
                return float(data2[0].get("last", 0))
    except Exception as e:
        print(f"  [WARN] get_current_price: {e}")
    return 0.0
 
 
def calc_lot_size(symbol, signal=""):
    if LOT_SIZE_FIXED > 0:
        return LOT_SIZE_FIXED
    price = get_current_price(symbol)
    if price <= 0:
        print(f"  [WARN] цена не получена, fallback 0.1")
        return 0.1
    risk  = SIGNAL_TYPES.get(signal, {}).get("risk", 1.0)
    lot_usd  = DEPOSIT * (LOT_PCT / 100.0) * risk
    lot_size = round(lot_usd / price, 3)
    print(f"  LOT: ${lot_usd:.2f} / {price} = {lot_size} ({signal}, risk x{risk})")
    return lot_size
 
 
def get_position(symbol):
    for ep in ["/futures/pending-position", "/futures/position"]:
        r = api_get(ep, {"market": symbol, "market_type": "FUTURES"})
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
                for p in data.get(key, []):
                    if p.get("market") == symbol:
                        return p
        return None
    return None
 
 
# ─── Леверидж — устанавливаем один раз, кэшируем ───
_leverage_cache = {}
 
def ensure_leverage(symbol):
    if _leverage_cache.get(symbol) == LEVERAGE:
        return
    r = api_post("/futures/adjust-position-leverage", {
        "market":      symbol,
        "market_type": "FUTURES",
        "leverage":    str(LEVERAGE),
    })
    code = r.get("code", -1)
    if code in (0, 3639):  # 3639 = уже установлен / позиция открыта
        _leverage_cache[symbol] = LEVERAGE
        print(f"  [LEV] {LEVERAGE}x для {symbol} OK (code={code})")
    else:
        print(f"  [WARN] leverage error: {r.get('message', r)}")
 
 
def place_order(symbol, side, amount):
    ensure_leverage(symbol)
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
        print(f"  [WARN] close_position: нет позиции {symbol}")
        _reset_state()
        return {"msg": "нет позиции"}
    side   = "sell" if pos.get("side") == "long" else "buy"
    amount = str(pos.get("close_avbl", pos.get("open_interest", pos.get("amount", "0"))))
    print(f"  close_position: {side} {amount}")
    result = api_post("/futures/order", {
        "market":         symbol,
        "market_type":    "FUTURES",
        "side":           side,
        "type":           "market",
        "amount":         amount,
        "close_position": True,
    })
    _reset_state()
    return result
 
 
def _reset_state():
    position_state.update({
        "dir": 0, "avg": 0.0, "lots": 0,
        "signal": "", "avwap_tp": 0.0, "power": 0, "zone": "",
    })
 
 
def _update_state(dir_, lots, avg, symbol, signal, power, zone, avwap_tp):
    position_state.update({
        "dir":      dir_,
        "avg":      avg,
        "lots":     lots,
        "symbol":   symbol,
        "signal":   signal,
        "power":    power,
        "zone":     zone,
        "avwap_tp": avwap_tp,
    })
 
 
# ─── Guardian ───
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
        loss_pct = ((avg - price) / avg * 100) if position_state["dir"] == 1 else ((price - avg) / avg * 100)
 
        if loss_pct >= MAX_LOSS_PCT:
            print(f"  [GUARDIAN/{source}] Убыток {loss_pct:.2f}% >= {MAX_LOSS_PCT}% — закрываю")
            result = close_position(symbol)
            log_signal({"action": "guardian_close", "symbol": symbol, "avg": str(avg), "signal": position_state.get("signal", "")},
                       result, extra={"loss_pct": round(loss_pct, 2), "source": source})
            return
 
        avwap_tp = position_state.get("avwap_tp", 0)
        if avwap_tp > 0:
            hit = (position_state["dir"] == 1 and price >= avwap_tp and avg < avwap_tp) or \
                  (position_state["dir"] == -1 and price <= avwap_tp and avg > avwap_tp)
            if hit:
                print(f"  [AVWAP TP/{source}] price={price} avwap={avwap_tp}")
                result = close_position(symbol)
                log_signal({"action": "avwap_tp", "symbol": symbol, "avg": str(avg), "signal": position_state.get("signal", "")}, result)
    except Exception as e:
        print(f"  [GUARDIAN ERROR/{source}] {e}")
 
 
def guardian_loop():
    print(f"[GUARDIAN] Поток запущен, интервал={GUARDIAN_INTERVAL}с")
    while True:
        time.sleep(GUARDIAN_INTERVAL)
        try:
            guardian_check(position_state.get("symbol", "SOLUSDT"), source="bg")
        except Exception as e:
            print(f"  [GUARDIAN LOOP] {e}")
 
if GUARDIAN_ENABLED:
    threading.Thread(target=guardian_loop, daemon=True).start()
 
 
# ─── Webhook ───
@app.route("/webhook", methods=["POST"])
def webhook():
    if request.args.get("token", "") != WEBHOOK_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "no json"}), 400
 
    action   = data.get("action", "").lower()
    symbol   = data.get("symbol", "SOLUSDT").upper()
    lots     = int(data.get("lots", 1))
    power    = int(data.get("power", 1))
    signal   = data.get("signal", "checklist")
    zone     = data.get("zone", "")
    avwap_tp = float(data.get("avwap_mid", 0) or 0)
 
    lot_size = calc_lot_size(symbol, signal)
    amount   = round(lot_size * lots, 3)
 
    print(f"\n[{time.strftime('%H:%M:%S')}] {action} | {symbol} | lots={lots} | power={power} | signal={signal} | amount={amount} | zone={zone}")
 
    guardian_check(symbol, source="pre-webhook")
 
    result = {"msg": "no action"}
 
    if action == "buy":
        pos = get_position(symbol)
        if pos and pos.get("side") == "short":
            print(f"  Закрываем шорт перед лонгом")
            close_position(symbol)
        result = place_order(symbol, "buy", amount)
        if isinstance(result, dict) and result.get("code") == 0:
            avg_val = float(data.get("avg", 0) or 0)
            fp      = result.get("data", {}).get("last_filled_price", 0)
            _update_state(1, lots, avg_val or float(fp), symbol, signal, power, zone, avwap_tp)
 
    elif action == "sell":
        pos = get_position(symbol)
        if pos and pos.get("side") == "long":
            print(f"  Закрываем лонг перед шортом")
            close_position(symbol)
        result = place_order(symbol, "sell", amount)
        if isinstance(result, dict) and result.get("code") == 0:
            avg_val = float(data.get("avg", 0) or 0)
            fp      = result.get("data", {}).get("last_filled_price", 0)
            _update_state(-1, lots, avg_val or float(fp), symbol, signal, power, zone, avwap_tp)
 
    elif action == "close_all":
        result = close_position(symbol)
 
    elif action == "reverse":
        print(f"  REVERSE: закрываем и открываем в другую сторону")
        close_position(symbol)
        new_side = data.get("new_side", "")
        if new_side in ("buy", "sell"):
            result = place_order(symbol, new_side, amount)
            if isinstance(result, dict) and result.get("code") == 0:
                avg_val = float(data.get("avg", 0) or 0)
                fp      = result.get("data", {}).get("last_filled_price", 0)
                _update_state(1 if new_side == "buy" else -1, lots, avg_val or float(fp), symbol, signal, power, zone, avwap_tp)
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
        print(f"  [INFO] {action} — только лог")
        result = {"msg": f"logged: {action}"}
 
    else:
        return jsonify({"error": f"unknown action: {action}"}), 400
 
    print(f"  ИТОГ: {result}")
    log_signal(data, result)
    return jsonify({"ok": True, "result": result})
 
 
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status":  "ok",
        "server":  "CoinEx Webhook v8",
        "state":   position_state,
        "config": {
            "deposit":           DEPOSIT,
            "lot_pct":           LOT_PCT,
            "leverage":          LEVERAGE,
            "lot_fixed":         LOT_SIZE_FIXED,
            "max_loss_pct":      MAX_LOSS_PCT,
            "guardian":          GUARDIAN_ENABLED,
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
    if request.args.get("token", "") != WEBHOOK_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    symbol = request.args.get("symbol", "SOLUSDT").upper()
    result = close_position(symbol)
    log_signal({"action": "manual_close", "symbol": symbol, "signal": "manual"}, result)
    return jsonify({"ok": True, "result": result})
 
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"CoinEx Webhook v8 | port={port} | deposit={DEPOSIT} | lot_pct={LOT_PCT}% | lev={LEVERAGE}x")
    print(f"guardian={GUARDIAN_ENABLED} | max_loss={MAX_LOSS_PCT}% | interval={GUARDIAN_INTERVAL}s")
    app.run(host="0.0.0.0", port=port)
