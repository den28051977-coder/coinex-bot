"""
CoinEx Futures Webhook Server v8.1
Изменения vs v8 (git):
- ИСПРАВЛЕН log_signal: вернут потерянный заголовок функции (был NameError на каждом вебхуке)
- Удалена мёртвая вторая копия (старый v7), которая висела после app.run()
- Персистентный CSV-лог по источникам (TRADE_LOG_CSV) — для анализа EV по signal
- Дедуп одинаковых алертов от двух пирамид (DEDUP_SEC, 0 = выкл)
- Reconcile guardian от биржи при потере состояния (GUARDIAN_RECONCILE)
"""

import os, hmac, hashlib, time, json, csv, requests, threading
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

# ─── Новое: лог, дедуп, reconcile ───
TRADE_LOG_CSV      = os.environ.get("TRADE_LOG_CSV", "trades_log.csv")
DEDUP_SEC          = float(os.environ.get("DEDUP_SEC", "4"))          # 0 = дедуп выкл
GUARDIAN_RECONCILE = os.environ.get("GUARDIAN_RECONCILE", "true").lower() == "true"

# ─── Telegram ───
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED   = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

BASE_URL = "https://api.coinex.com"
signals_log = deque(maxlen=200)
market_state = None  # последний heartbeat от Pine (живая методичка): цена/зоны/уровни/дельты

# Защита от гонок: guardian-поток и webhook могут писать лог одновременно
_log_lock    = threading.Lock()
# Состояние дедупа последнего действия
_last_action = {"key": "", "ts": 0.0}

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

# Колонки CSV-лога (фиксированный набор, лишние ключи игнорируются)
CSV_FIELDS = ["time", "action", "signal", "signal_label", "zone", "zone_h1", "zone_h4", "trend",
              "delta_day", "delta_range", "last_extreme", "week_hi", "week_lo", "pwh", "pwl",
              "avwap", "pdh", "pdl",
              "power", "lots", "avg", "filled_price", "pnl", "loss_pct",
              "source", "result"]


def send_telegram(text):
    """Отправляет сообщение в Telegram. Не бросает исключения наружу —
    если Telegram не настроен или недоступен, просто логирует ошибку
    в консоль и продолжает работу бота (торговля не должна зависеть
    от доступности Telegram)."""
    if not TELEGRAM_ENABLED:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=5)
        if resp.status_code != 200:
            print(f"  [TELEGRAM] Ошибка отправки: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"  [TELEGRAM] Исключение при отправке: {e}")


def f_signal_label(signal):
    return SIGNAL_TYPES.get(signal, {"label": signal}).get("label", signal)


def _csv_append(entry):
    """Дописывает строку в CSV-лог. Никогда не бросает наружу — сбой записи
    лога не должен влиять на торговлю."""
    try:
        new_file = not os.path.exists(TRADE_LOG_CSV)
        with open(TRADE_LOG_CSV, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if new_file:
                w.writeheader()
            w.writerow(entry)
    except Exception as e:
        print(f"  [CSV] Ошибка записи: {e}")


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
        "zone_h1":      data.get("zone_h1", ""),
        "zone_h4":      data.get("zone_h4", ""),
        "trend":        data.get("trend", ""),
        "delta_day":    data.get("delta_day", ""),
        "delta_range":  data.get("delta_range", ""),
        "last_extreme": data.get("last_extreme", ""),
        "week_hi":      data.get("week_hi", ""),
        "week_lo":      data.get("week_lo", ""),
        "pwh":          data.get("pwh", ""),
        "pwl":          data.get("pwl", ""),
        "avwap":        data.get("avwap", ""),
        "pdh":          data.get("pdh", ""),
        "pdl":          data.get("pdl", ""),
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
    with _log_lock:
        signals_log.append(entry)
        _csv_append(entry)


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


# ─────────────────────────────────────────────────────────────
#  МОДУЛЬ УРОВНЕЙ (Binance — там ликвидность SOL)
#  День: PDH/PDL/DO · Неделя вс→вс: Week Hi/Lo, PWH/PWL, WO
#  Кэш 15 мин (уровни медленные, API не дёргаем часто)
# ─────────────────────────────────────────────────────────────
import time as _time
from datetime import datetime, timezone, timedelta

_levels_cache = {"ts": 0, "data": None}
_LEVELS_TTL = 15 * 60  # 15 минут

def get_binance_klines(symbol="SOLUSDT", interval="1d", limit=60):
    """Свечи с Binance (публичный API, без подписи). [[openTime,o,h,l,c,v,closeTime,...]]"""
    try:
        url = "https://api.binance.com/api/v3/klines"
        r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
        if r.status_code == 200:
            return r.json()
        print(f"  [LEVELS] binance klines {interval} → HTTP {r.status_code}")
    except Exception as e:
        print(f"  [LEVELS] klines error: {e}")
    return []

def _week_start_sunday(ts):
    """Timestamp начала крипто-недели (воскресенье 00:00 UTC) для данного ts."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    days_since_sunday = (dt.weekday() + 1) % 7  # Вс=0, Пн=1..Сб=6
    ws = dt.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_sunday)
    return int(ws.timestamp())

def calc_levels(symbol="SOLUSDT"):
    """Считает уровни день/неделя. Кэш 15 мин."""
    now = _time.time()
    if _levels_cache["data"] is not None and (now - _levels_cache["ts"]) < _LEVELS_TTL:
        return _levels_cache["data"]

    out = {"updated": int(now)}
    # дневные свечи (последние 70 дней — хватит на дни/недели вс→вс)
    days = get_binance_klines(symbol, "1d", 70)
    if days and len(days) >= 2:
        # [openTime(ms),o,h,l,c,v,...]; последняя свеча = сегодня (формируется)
        def H(k): return float(k[2])
        def L(k): return float(k[3])
        def O(k): return float(k[1])
        # ДЕНЬ
        out["DO"]  = O(days[-1])           # открытие сегодня
        out["PDH"] = H(days[-2])           # вчерашний high
        out["PDL"] = L(days[-2])           # вчерашний low
        # НЕДЕЛЯ вс→вс — группируем дневные свечи по крипто-неделям
        cur_ws = _week_start_sunday(int(days[-1][0] / 1000))
        prev_ws = cur_ws - 7 * 86400
        cur_days, prev_days = [], []
        for k in days:
            kt = int(k[0] / 1000)
            kws = _week_start_sunday(kt)
            if kws == cur_ws:
                cur_days.append(k)
            elif kws == prev_ws:
                prev_days.append(k)
        if cur_days:
            out["week_hi"] = max(H(k) for k in cur_days)
            out["week_lo"] = min(L(k) for k in cur_days)
            out["WO"]      = O(cur_days[0])    # открытие недели (воскресенье)
        if prev_days:
            out["PWH"] = max(H(k) for k in prev_days)
            out["PWL"] = min(L(k) for k in prev_days)

    _levels_cache["data"] = out
    _levels_cache["ts"] = now
    print(f"  [LEVELS] обновлены: {out}")
    return out


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


def _reconcile_from_exchange(symbol):
    """Если состояние потеряно (рестарт/рассинхрон), но на бирже есть позиция —
    восстанавливаем dir/avg, чтобы guardian мог защитить. Только при надёжной
    цене входа (>0); иначе ничего не делаем (безопасный no-op)."""
    try:
        pos = get_position(symbol)
        if not pos:
            return
        side = pos.get("side")
        ep = None
        for k in ("avg_entry_price", "entry_price", "open_price", "settle_price", "avg_price"):
            v = pos.get(k)
            if v:
                try:
                    ep = float(v)
                except (TypeError, ValueError):
                    ep = None
                if ep and ep > 0:
                    break
        if side in ("long", "short") and ep and ep > 0:
            position_state["dir"] = 1 if side == "long" else -1
            position_state["avg"] = ep
            print(f"  [GUARDIAN RECONCILE] восстановлено: dir={position_state['dir']} avg={ep}")
    except Exception as e:
        print(f"  [GUARDIAN RECONCILE ERROR] {e}")


def guardian_check(symbol, source="webhook"):
    if not GUARDIAN_ENABLED or MAX_LOSS_PCT <= 0:
        return
    # Reconcile: состояние пустое, но позиция на бирже есть → восстановим
    if GUARDIAN_RECONCILE and (position_state["dir"] == 0 or position_state["avg"] <= 0):
        _reconcile_from_exchange(symbol)
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


def _is_duplicate(action, signal, data, lots):
    """True, если такой же алерт пришёл в окне DEDUP_SEC (защита от двух
    пирамид/двойного flip+breakout на одной свече). Мутирует _last_action."""
    if DEDUP_SEC <= 0:
        return False
    dkey = f"{action}:{signal}:{data.get('avg','')}:{lots}:{data.get('reason','')}"
    now_ts = time.time()
    if dkey == _last_action["key"] and (now_ts - _last_action["ts"]) < DEDUP_SEC:
        return True
    _last_action["key"] = dkey
    _last_action["ts"]  = now_ts
    return False


@app.route("/webhook", methods=["POST"])
def webhook():
    if request.args.get("token", "") != WEBHOOK_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "no json"}), 400

    action = data.get("action", "").lower()
    symbol = data.get("symbol", "SOLUSDT").upper()

    # ── HEARTBEAT: факты рынка раз в бар (живая методичка) ──
    # Не торгует, не пишет в signals_log — только обновляет market_state.
    if action == "heartbeat":
        global market_state
        market_state = {
            "updated":      int(time.time()),
            "price":        data.get("price", ""),
            "zone":         data.get("zone", ""),
            "zone_h1":      data.get("zone_h1", ""),
            "zone_h4":      data.get("zone_h4", ""),
            "week_hi":      data.get("week_hi", ""),
            "week_lo":      data.get("week_lo", ""),
            "pwh":          data.get("pwh", ""),
            "pwl":          data.get("pwl", ""),
            "pdh":          data.get("pdh", ""),
            "pdl":          data.get("pdl", ""),
            "avwap":        data.get("avwap", ""),
            "delta_day":    data.get("delta_day", ""),
            "delta_sess":   data.get("delta_sess", ""),
            "delta_range":  data.get("delta_range", ""),
            "last_extreme": data.get("last_extreme", ""),
            "trend":        data.get("trend", ""),
            "came_from":    data.get("came_from", ""),
            "btc_conflict": data.get("btc_conflict", 0),
        }
        return jsonify({"ok": True, "heartbeat": True})

    lots   = int(data.get("lots", 1))
    power  = int(data.get("power", 1))
    signal = data.get("signal", "checklist")

    # ── Дедуп одинаковых алертов от двух пирамид ──
    if action in ("buy", "sell", "close_all", "reverse", "unload") and _is_duplicate(action, signal, data, lots):
        print(f"  [DEDUP] Пропуск дубля: {action}/{signal}/avg={data.get('avg','')}/lots={lots}")
        log_signal(data, {"msg": "deduped"}, extra={"source": "dedup"})
        return jsonify({"ok": True, "deduped": True})

    lot_size = calc_lot_size(symbol, signal)
    amount   = round(lot_size * lots, 3)

    print(f"\n[{time.strftime('%H:%M:%S')}] ACTION={action} | {symbol} | lots={lots} | power={power} | signal={signal} | amount={amount} | zone={data.get('zone','')} | zone_h1={data.get('zone_h1','')} | trend={data.get('trend','')} | dDay={data.get('delta_day','')} | dRange={data.get('delta_range','')} | lExt={data.get('last_extreme','')}")

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
            entry_price = avg_val if avg_val > 0 else float(fp or 0)
            position_state.update({
                "dir":      1,
                "avg":      entry_price,
                "lots":     lots,
                "symbol":   symbol,
                "signal":   signal,
                "power":    power,
                "zone":     data.get("zone", ""),
                "avwap_tp": float(data.get("avwap_mid", 0) or 0),
            })
            send_telegram(
                f"🟢 <b>ЛОНГ открыт</b>\n"
                f"Символ: {symbol}\n"
                f"Лоты: {lots} | Сила: {power}\n"
                f"Сигнал: {f_signal_label(signal)}\n"
                f"Цена входа: {entry_price:.4f}\n"
                f"Зона: {data.get('zone','—')}"
            )

    elif action == "sell":
        pos = get_position(symbol)
        if pos and pos.get("side") == "long":
            print(f"  [INFO] Открыт лонг — закрываем перед шортом")
            close_position(symbol)
        result = place_order(symbol, "sell", amount)
        if isinstance(result, dict) and result.get("code") == 0:
            fp = result.get("data", {}).get("last_filled_price")
            avg_val = float(data.get("avg", 0) or 0)
            entry_price = avg_val if avg_val > 0 else float(fp or 0)
            position_state.update({
                "dir":      -1,
                "avg":      entry_price,
                "lots":     lots,
                "symbol":   symbol,
                "signal":   signal,
                "power":    power,
                "zone":     data.get("zone", ""),
                "avwap_tp": float(data.get("avwap_mid", 0) or 0),
            })
            send_telegram(
                f"🔴 <b>ШОРТ открыт</b>\n"
                f"Символ: {symbol}\n"
                f"Лоты: {lots} | Сила: {power}\n"
                f"Сигнал: {f_signal_label(signal)}\n"
                f"Цена входа: {entry_price:.4f}\n"
                f"Зона: {data.get('zone','—')}"
            )

    elif action == "close_all":
        old_dir  = position_state.get("dir", 0)
        old_avg  = position_state.get("avg", 0.0)
        old_lots = position_state.get("lots", 0)
        cur_price = get_current_price(symbol)
        result = close_position(symbol)
        if old_dir != 0 and old_avg > 0 and cur_price:
            pnl_pct = (cur_price - old_avg) / old_avg * 100 * old_dir
            emoji = "✅" if pnl_pct >= 0 else "❌"
            send_telegram(
                f"{emoji} <b>Позиция закрыта</b>\n"
                f"Символ: {symbol}\n"
                f"Направление: {'ЛОНГ' if old_dir == 1 else 'ШОРТ'}\n"
                f"Лоты: {old_lots}\n"
                f"Причина: {data.get('reason', data.get('signal',''))}\n"
                f"PnL: {pnl_pct:+.2f}%"
            )
        else:
            send_telegram(f"ℹ️ Закрытие позиции {symbol} (причина: {data.get('reason', data.get('signal',''))})")

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
                entry_price = avg_val if avg_val > 0 else float(fp or 0)
                position_state.update({
                    "dir":      1 if new_side == "buy" else -1,
                    "avg":      entry_price,
                    "lots":     lots,
                    "symbol":   symbol,
                    "signal":   signal,
                    "power":    power,
                    "zone":     data.get("zone", ""),
                    "avwap_tp": float(data.get("avwap_mid", 0) or 0),
                })
                send_telegram(
                    f"🔄 <b>РАЗВОРОТ</b>\n"
                    f"Символ: {symbol}\n"
                    f"Новое направление: {'ЛОНГ' if new_side == 'buy' else 'ШОРТ'}\n"
                    f"Лоты: {lots} | Сила: {power}\n"
                    f"Сигнал: {f_signal_label(signal)}\n"
                    f"Цена входа: {entry_price:.4f}"
                )
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

    elif action == "info":
        # Информационный сигнал без торгового действия (например,
        # обеденный разворот NY Lunch Judas) — только уведомление в Telegram
        note = data.get("note", "")
        level = data.get("level", "")
        price = data.get("price", "")
        print(f"  [INFO] {signal}: {note}")
        send_telegram(
            f"📢 <b>{f_signal_label(signal)}</b>\n"
            f"{note}\n"
            f"Уровень: {level} | Цена: {price}"
        )
        result = {"msg": f"info logged: {signal}"}

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
        "server":   "CoinEx Webhook v8.1",
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
            "guardian_reconcile": GUARDIAN_RECONCILE,
            "dedup_sec":        DEDUP_SEC,
            "trade_log_csv":    TRADE_LOG_CSV,
        }
    })


@app.route("/position/<symbol>", methods=["GET"])
def check_position(symbol):
    pos = get_position(symbol.upper())
    return jsonify({"position": pos, "state": position_state})


@app.route("/state", methods=["GET"])
def state():
    # Живое состояние рынка из heartbeat (Pine раз в бар). None если heartbeat ещё не пришёл.
    return jsonify(market_state or {"waiting": "no heartbeat yet"})


@app.route("/levels", methods=["GET"])
def levels():
    # Уровни из heartbeat (свежие, не зависят от входов). Фолбэк — последний вход из лога.
    if market_state:
        return jsonify({k: market_state.get(k) for k in
                        ("week_hi", "week_lo", "pwh", "pwl", "pdh", "pdl", "avwap", "updated")})
    out = {"source": "pine_payload"}
    try:
        for s in reversed(signals_log):
            for k in ("week_hi", "week_lo", "pwh", "pwl"):
                if k not in out and s.get(k) not in (None, ""):
                    out[k] = s.get(k)
            if all(k in out for k in ("week_hi", "week_lo", "pwh", "pwl")):
                break
    except Exception as e:
        out["error"] = str(e)
    return jsonify(out)


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
    print(f"DEDUP_SEC={DEDUP_SEC}, RECONCILE={GUARDIAN_RECONCILE}, CSV={TRADE_LOG_CSV}")
    app.run(host="0.0.0.0", port=port)
