/* =====================================================================
   Hedge-Range v2 — мульти-биржевой funding-релей
   ---------------------------------------------------------------------
   Что делает:
     Опрашивает 5 бирж (Bybit, OKX, Bitget, MEXC, CoinEx) по обеим
     сторонам (USDT-перп и USDC-перп) и отдаёт таблицу перекоса
     funding: netEdge = fundingUSDC − fundingUSDT.
     Чем жирнее netEdge — тем выгоднее там стрэддл.

   ЗАПУСК:
     1. Node.js 22+ (нужен нативный fetch). Проверка: node -v
     2. В терминале, из этой папки:  node relay-v2.js
     3. Открой в браузере: http://localhost:8788/
     4. Стоп: Ctrl + C

   НЕ ЗАВИСИТ от текущего relay.js — свой процесс, свой порт (8788).
   Зависимостей нет — только встроенные модули Node 22+.
   ===================================================================== */

"use strict";

const http = require("http");
const fs   = require("fs");
const path = require("path");

const PORT = 8788;
const HOST = "127.0.0.1";
const HTML_FILE = path.join(__dirname, "hedge-v2.html");

/* ────────────────────────────────────────────────────────────────────
   keys.env — конфигурация торговли (порт из coinex_webhook_server.py)
   Все параметры опциональны. Без ключей CoinEx торговые эндпоинты
   вернут "нет ключей" — просмотр funding/свечей продолжает работать.
   ──────────────────────────────────────────────────────────────────── */
function loadEnv() {
  try {
    const p = path.join(__dirname, "keys.env");
    if (!fs.existsSync(p)) return false;
    for (const line of fs.readFileSync(p, "utf8").split(/\r?\n/)) {
      const s = line.trim();
      if (!s || s.startsWith("#")) continue;
      const i = s.indexOf("=");
      if (i < 0) continue;
      const k = s.slice(0, i).trim();
      let v = s.slice(i + 1).trim();
      if ((v[0] === '"' && v.endsWith('"')) || (v[0] === "'" && v.endsWith("'"))) {
        v = v.slice(1, -1);
      }
      if (!process.env[k]) process.env[k] = v;
    }
    return true;
  } catch (e) { return false; }
}
loadEnv();

// ─── Railway (torговый прокси) ─────────────────────────────────────
// Ключи CoinEx лежат на Railway, туда IP-whitelist. Локальный релей
// не хранит ключи — он проксирует все торговые вызовы на Railway.
const RAILWAY_URL   = process.env.RAILWAY_URL || "https://coinex-bot-production.up.railway.app";
const WEBHOOK_TOKEN = process.env.WEBHOOK_TOKEN || "mytoken123"; // такой же как в Railway Variables
const LEVERAGE      = +(process.env.LEVERAGE || 10);            // только для отображения; реально на Railway

// funding обновляется биржей раз в 8ч, кэш на 30с достаточно чтобы
// не долбить их API и не ловить rate-limit при частых refresh фронта
const CACHE = new Map(); // key = `${ex}:${base}:${quote}` → {val,ts}
const CACHE_TTL_MS = 30_000;

/* ────────────────────────────────────────────────────────────────────
   HTTP helpers
   ──────────────────────────────────────────────────────────────────── */

function fetchTO(url, ms = 8000, opts = {}) {
  const c = new AbortController();
  const t = setTimeout(() => c.abort(), ms);
  return fetch(url, { signal: c.signal, cache: "no-store", ...opts })
    .finally(() => clearTimeout(t));
}

async function jget(url, ms = 8000) {
  const r = await fetchTO(url, ms);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

/* ────────────────────────────────────────────────────────────────────
   Биржевые фетчеры funding
   Каждая функция принимает (base, quote) → { rate, nextFundingTime, mark }
   quote: 'USDT' | 'USDC'
   Ошибка — throw. rate в долях (например 0.0001 = 0.01% за расчёт).
   ──────────────────────────────────────────────────────────────────── */

// ─── Bybit ────────────────────────────────────────────────────────
// USDT-linear: symbol = {BASE}USDT       (category=linear)
// USDC-linear: symbol = {BASE}PERP       (Perpetual USDC — уникальный формат)
async function fundBybit(base, quote) {
  const sym = quote === "USDT" ? base + "USDT" : base + "PERP";
  const j = await jget(`https://api.bybit.com/v5/market/tickers?category=linear&symbol=${sym}`);
  if (j.retCode !== 0) throw new Error(j.retMsg || "bybit err");
  const t = j.result?.list?.[0];
  if (!t) throw new Error("no data");
  const fr = +t.fundingRate;
  if (!isFinite(fr)) throw new Error("nan");
  return {
    rate: fr,
    nextFundingTime: +t.nextFundingTime || null,
    mark: +t.markPrice || null,
  };
}

// ─── OKX ──────────────────────────────────────────────────────────
// USDT SWAP: instId = {BASE}-USDT-SWAP
// USDC SWAP: instId = {BASE}-USDC-SWAP  (доступно не для всех монет)
async function fundOkx(base, quote) {
  const inst = `${base}-${quote}-SWAP`;
  const j = await jget(`https://www.okx.com/api/v5/public/funding-rate?instId=${inst}`);
  if (j.code !== "0") throw new Error(j.msg || "okx err");
  const d = j.data?.[0];
  if (!d) throw new Error("no data");
  const fr = +d.fundingRate;
  if (!isFinite(fr)) throw new Error("nan");
  return {
    rate: fr,
    nextFundingTime: +d.nextFundingTime || null,
    mark: null,
  };
}

// ─── Bitget ───────────────────────────────────────────────────────
// v2 mix: symbol = {BASE}{QUOTE}, productType = usdt-futures | usdc-futures
async function fundBitget(base, quote) {
  const productType = quote === "USDT" ? "usdt-futures" : "usdc-futures";
  const sym = `${base}${quote}`;
  const j = await jget(`https://api.bitget.com/api/v2/mix/market/current-fund-rate?symbol=${sym}&productType=${productType}`);
  if (j.code !== "00000") throw new Error(j.msg || "bitget err");
  const d = Array.isArray(j.data) ? j.data[0] : j.data;
  const fr = +(d?.fundingRate ?? d?.rate);
  if (!isFinite(fr)) throw new Error("nan");
  return { rate: fr, nextFundingTime: null, mark: null };
}

// ─── MEXC ─────────────────────────────────────────────────────────
// contract: symbol = {BASE}_{QUOTE}  (XRP_USDT / XRP_USDC)
async function fundMexc(base, quote) {
  const sym = `${base}_${quote}`;
  const j = await jget(`https://contract.mexc.com/api/v1/contract/funding_rate/${sym}`);
  if (!j.success) throw new Error(j.message || j.msg || "mexc err");
  const fr = +j.data?.fundingRate;
  if (!isFinite(fr)) throw new Error("nan");
  return {
    rate: fr,
    nextFundingTime: +j.data?.nextSettleTime || null,
    mark: null,
  };
}

// ─── CoinEx ───────────────────────────────────────────────────────
// v2 futures: market = {BASE}{QUOTE}  (XRPUSDT / XRPUSDC)
async function fundCoinex(base, quote) {
  const sym = `${base}${quote}`;
  const j = await jget(`https://api.coinex.com/v2/futures/funding-rate?market=${sym}`);
  if (j.code !== 0) throw new Error(j.message || "coinex err");
  const d = Array.isArray(j.data) ? j.data[0] : j.data;
  const fr = +(d?.latest_funding_rate ?? d?.funding_rate ?? d?.next_funding_rate);
  if (!isFinite(fr)) throw new Error("nan");
  return { rate: fr, nextFundingTime: null, mark: null };
}

const EXCHANGES = {
  bybit:  fundBybit,
  okx:    fundOkx,
  bitget: fundBitget,
  mexc:   fundMexc,
  coinex: fundCoinex,
};

/* ────────────────────────────────────────────────────────────────────
   Свечи (Bybit v5 kline linear USDT) — для расчёта ATR-ренжа
   Одной биржи достаточно: цена везде почти одинаковая, а funding
   мы уже собираем со всех отдельно.
   ──────────────────────────────────────────────────────────────────── */

const CANDLE_CACHE = new Map(); // `${base}:${interval}:${limit}` → {val,ts}
const CANDLE_TTL_MS = 60_000;   // свечи 15m обновлять раз в минуту достаточно

// Bybit kline: [start, open, high, low, close, volume, turnover] — newest first
async function fetchCandlesBybit(base, interval = "15", limit = 300) {
  const sym = base + "USDT";
  const j = await jget(
    `https://api.bybit.com/v5/market/kline?category=linear&symbol=${sym}&interval=${interval}&limit=${limit}`
  );
  if (j.retCode !== 0) throw new Error(j.retMsg || "bybit kline err");
  const list = j.result?.list || [];
  return list.slice().reverse().map(k => ({
    t: +k[0], o: +k[1], h: +k[2], l: +k[3], c: +k[4], v: +k[5],
  }));
}

async function getCandles(base, interval = "15", limit = 300) {
  const key = `${base}:${interval}:${limit}`;
  const cached = CANDLE_CACHE.get(key);
  if (cached && Date.now() - cached.ts < CANDLE_TTL_MS) return cached.val;
  try {
    const v = await fetchCandlesBybit(base, interval, limit);
    CANDLE_CACHE.set(key, { val: v, ts: Date.now() });
    return v;
  } catch (e) {
    CANDLE_CACHE.set(key, { val: null, ts: Date.now(), err: String(e.message || e) });
    return null;
  }
}

/* ────────────────────────────────────────────────────────────────────
   Кэш + сборка таблицы
   ──────────────────────────────────────────────────────────────────── */

async function getFunding(ex, base, quote) {
  const key = `${ex}:${base}:${quote}`;
  const cached = CACHE.get(key);
  if (cached && Date.now() - cached.ts < CACHE_TTL_MS) return cached.val;
  try {
    const v = await EXCHANGES[ex](base, quote);
    CACHE.set(key, { val: v, ts: Date.now() });
    return v;
  } catch (e) {
    const v = { rate: null, error: String(e.message || e) };
    CACHE.set(key, { val: v, ts: Date.now() });
    return v;
  }
}

// { [base]: { [ex]: { usdt, usdc, netEdge, err?, usdtNext, usdcNext } } }
async function scanFundingMulti(bases) {
  const out = {};
  const tasks = [];
  for (const base of bases) {
    out[base] = {};
    for (const ex of Object.keys(EXCHANGES)) {
      out[base][ex] = { usdt: null, usdc: null, netEdge: null };
      tasks.push((async () => {
        const [u, c] = await Promise.all([
          getFunding(ex, base, "USDT"),
          getFunding(ex, base, "USDC"),
        ]);
        out[base][ex].usdt = u.rate;
        out[base][ex].usdc = c.rate;
        const errs = [u.error, c.error].filter(Boolean);
        if (errs.length) out[base][ex].err = errs.join(" | ");
        if (u.rate != null && c.rate != null) {
          out[base][ex].netEdge = c.rate - u.rate;
        }
        out[base][ex].usdtNext = u.nextFundingTime || null;
        out[base][ex].usdcNext = c.nextFundingTime || null;
        if (u.mark != null || c.mark != null) {
          out[base][ex].mark = u.mark ?? c.mark;
        }
      })());
    }
  }
  await Promise.allSettled(tasks);
  return out;
}

/* ────────────────────────────────────────────────────────────────────
   Railway proxy — торговые вызовы идут на coinex_webhook_server.py
   (там ключи, там IP-whitelist CoinEx). Локальный релей ничего сам
   не подписывает и в CoinEx напрямую не ходит.
   Эндпоинты Railway:
     POST /straddle?token=X        {base, amount}
     POST /straddle-tpsl?token=X   {base, stop_pct, take_pct, entry_usdt?, entry_usdc?}
     POST /close?token=X&symbol=Y
     GET  /position/{symbol}
   ──────────────────────────────────────────────────────────────────── */

const round6 = x => Math.round(x * 1e6) / 1e6;

// Railway free-tier может спать и просыпаться 20-40 сек. Даём щедрый таймаут.
const RAILWAY_TIMEOUT_MS = 45_000;

async function railwayPost(apiPath, payload, token, extraQuery = "") {
  const sep = apiPath.includes("?") ? "&" : "?";
  const url = RAILWAY_URL + apiPath + sep + "token=" + encodeURIComponent(token)
              + (extraQuery ? "&" + extraQuery : "");
  try {
    const r = await fetchTO(url, RAILWAY_TIMEOUT_MS, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(payload || {}),
    });
    const j = await r.json().catch(() => ({ error: "bad json", status: r.status }));
    console.log(`  POST Railway${apiPath} → ${r.status}: ${JSON.stringify(j).slice(0,200)}`);
    return j;
  } catch (e) {
    const msg = e.name === "AbortError"
      ? `Railway не ответил за ${RAILWAY_TIMEOUT_MS/1000}с (возможно спит — попробуй ещё раз через 30с, free-tier просыпается ~40с)`
      : `Railway error: ${e.message}`;
    console.log(`  POST Railway${apiPath} → ✗ ${msg}`);
    return { error: msg };
  }
}

async function railwayGet(apiPath) {
  try {
    const r = await fetchTO(RAILWAY_URL + apiPath, RAILWAY_TIMEOUT_MS, { method: "GET" });
    const j = await r.json().catch(() => ({}));
    return j;
  } catch (e) {
    const msg = e.name === "AbortError"
      ? `Railway timeout (${RAILWAY_TIMEOUT_MS/1000}с) — free-tier мог заснуть, попробуй ещё раз`
      : `Railway error: ${e.message}`;
    console.log(`  GET Railway${apiPath} → ✗ ${msg}`);
    return { error: msg };
  }
}

// Открыть стрэддл (обе ноги market): возвращает {ok, status, long_usdt, short_usdc, warning}
async function railwayOpenStraddle(base, amount, token) {
  return railwayPost("/straddle", { base, amount }, token);
}

// TP/SL на обе ноги по процентам (зеркально): entry_usdt/usdc опциональны
async function railwayStraddleTpsl(base, stopPct, takePct, token, entries = {}) {
  const payload = { base, stop_pct: stopPct, take_pct: takePct };
  if (entries.entryUsdt) payload.entry_usdt = entries.entryUsdt;
  if (entries.entryUsdc) payload.entry_usdc = entries.entryUsdc;
  return railwayPost("/straddle-tpsl", payload, token);
}

// Закрыть одну позицию по символу (BASE+QUOTE)
async function railwayClose(base, quote, token) {
  const symbol = base + quote;
  return railwayPost(`/close?symbol=${symbol}`, {}, token);
}

// Прочитать позицию (публичный GET, без токена)
async function railwayGetPosition(base, quote) {
  const symbol = base + quote;
  const j = await railwayGet(`/position/${symbol}`);
  return j.position || null;
}

async function railwayGetPositionEntry(base, quote) {
  const p = await railwayGetPosition(base, quote);
  if (!p) return 0;
  for (const k of ["avg_entry_price", "entry_price", "open_price", "avg_price"]) {
    if (p[k]) {
      const v = +p[k];
      if (v > 0) return v;
    }
  }
  return 0;
}

async function railwayHealthCheck() {
  try {
    const j = await railwayGet("/");
    return { ok: true, server: j.server || "unknown" };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/* ────────────────────────────────────────────────────────────────────
   ATR-walkRange (порт из hedge-v2.html) — для state-machine автомата
   ──────────────────────────────────────────────────────────────────── */

function computeTR(candles) {
  const trs = new Array(candles.length).fill(null);
  for (let i = 1; i < candles.length; i++) {
    const c = candles[i], p = candles[i - 1];
    trs[i] = Math.max(c.h - c.l, Math.abs(c.h - p.c), Math.abs(c.l - p.c));
  }
  return trs;
}
function computeAtrRma(candles, len) {
  const trs = computeTR(candles);
  const out = new Array(candles.length).fill(null);
  let atr = null;
  for (let i = 1; i < candles.length; i++) {
    if (i < len) continue;
    if (atr == null) {
      let s = 0;
      for (let j = i - len + 1; j <= i; j++) s += trs[j];
      atr = s / len;
    } else {
      atr = (atr * (len - 1) + trs[i]) / len;
    }
    out[i] = atr;
  }
  return out;
}
function computeSmaSeries(arr, period) {
  const out = new Array(arr.length).fill(null);
  const q = []; let sum = 0;
  for (let i = 0; i < arr.length; i++) {
    const v = arr[i];
    if (v == null) continue;
    q.push(v); sum += v;
    if (q.length > period) sum -= q.shift();
    if (q.length === period) out[i] = sum / period;
  }
  return out;
}
function walkRange(candles, opts = {}) {
  const atrLen   = opts.atrLen   || 200;
  const smaLen   = opts.smaLen   || 100;
  const multi    = opts.multi    || 4;
  const maxOut   = opts.maxOut   || 100;
  const startBar = opts.startBar || 301;

  const atrs = computeAtrRma(candles, atrLen);
  const atrSmooth = computeSmaSeries(atrs, smaLen);
  const n = candles.length;
  const state = new Array(n).fill(null);
  const events = [];
  let value = null, upper = null, lower = null, upperMid = null, lowerMid = null;
  let count = 0, justReset = false;

  for (let i = 0; i < n; i++) {
    const c = candles[i];
    const atrW = atrSmooth[i] != null ? atrSmooth[i] * multi : null;

    if (value == null) {
      if (i >= startBar && atrW != null) {
        const hl2 = (c.h + c.l) / 2;
        value = hl2; upper = hl2 + atrW; lower = hl2 - atrW;
        upperMid = (value + upper) / 2; lowerMid = (value + lower) / 2;
        state[i] = { value, upper, lower, upperMid, lowerMid };
        justReset = true;
      }
      continue;
    }

    const p = candles[i - 1];
    const crossUp = p && p.l <= upper && c.l >  upper;
    const crossDn = p && p.h >= lower && c.h <  lower;

    if (c.l > upper || c.h < lower) count++;

    const doReset = crossUp || crossDn || count >= maxOut;
    if (doReset) {
      if (crossUp) events.push({ bar: i, type: "reset-up", price: upper });
      if (crossDn) events.push({ bar: i, type: "reset-dn", price: lower });
      count = 0;
      if (atrW != null) {
        const hl2 = (c.h + c.l) / 2;
        value = hl2; upper = hl2 + atrW; lower = hl2 - atrW;
        upperMid = (value + upper) / 2; lowerMid = (value + lower) / 2;
      }
      justReset = true;
    } else {
      justReset = false;
    }
    state[i] = { value, upper, lower, upperMid, lowerMid, reset: doReset };
  }
  let cur = null;
  for (let i = n - 1; i >= 0; i--) { if (state[i]) { cur = state[i]; break; } }
  const last = candles[n - 1];
  const range = cur ? {
    upper: cur.upper, lower: cur.lower, mid: cur.value,
    upperMid: cur.upperMid, lowerMid: cur.lowerMid,
    width: (cur.upper - cur.lower) / 2,
    close: last.c, posPct: (last.c - cur.lower) / (cur.upper - cur.lower),
  } : null;
  return { state, events, range };
}

/* ────────────────────────────────────────────────────────────────────
   State-machine автомата стрэддла
   States: WAIT_BREAK → WAIT_MID → STRADDLE_OPEN → SINGLE_LEG → DONE
   ──────────────────────────────────────────────────────────────────── */

const CYCLES = new Map(); // id → cycle
let cycleSeq = 0;

function newCycleId(base, ex) {
  cycleSeq++;
  return `${base}-${ex}-${Date.now().toString(36)}-${cycleSeq}`;
}

function cycleLog(cycle, type, msg, data) {
  if (!cycle.events) cycle.events = [];
  const e = { ts: Date.now(), type, msg };
  if (data) e.data = data;
  cycle.events.push(e);
  if (cycle.events.length > 100) cycle.events.shift();
  cycle.updatedAt = Date.now();
  console.log(`  [cycle ${cycle.base}/${cycle.ex}] ${cycle.state} · ${type}: ${msg}`);
}

function captureRange(st) {
  return {
    upper: st.upper, lower: st.lower, mid: st.value,
    upperMid: st.upperMid, lowerMid: st.lowerMid,
    width: (st.upper - st.lower) / 2,
  };
}

async function openStraddleInternal(base, amount, token) {
  // Railway делает оба ордера параллельно внутри своим тредингом
  const j = await railwayOpenStraddle(base, amount, token);
  // Railway ответ: {ok, status:"both_open|partial|failed", long_usdt, short_usdc, warning}
  await new Promise(r => setTimeout(r, 700)); // задержка на прописывание позиции
  const entryUsdt = j.status === "both_open" || j.long_usdt?.code === 0
                    ? await railwayGetPositionEntry(base, "USDT").catch(() => 0) : 0;
  const entryUsdc = j.status === "both_open" || j.short_usdc?.code === 0
                    ? await railwayGetPositionEntry(base, "USDC").catch(() => 0) : 0;
  return { status: j.status || "failed", entryUsdt, entryUsdc, raw: j };
}

async function evaluateCycle(cycle) {
  const cs = await getCandles(cycle.base, cycle.tf, 500);
  if (!cs || cs.length < 305) return;
  const walk = walkRange(cs);
  const price = cs[cs.length - 1].c;
  cycle.lastPrice = price;

  switch (cycle.state) {
    case "WAIT_BREAK": {
      // Ищем reset-up/reset-dn НОВЕЕ момента запуска цикла
      const resets = walk.events.filter(e =>
        cs[e.bar] && cs[e.bar].t >= cycle.createdAt &&
        (e.type === "reset-up" || e.type === "reset-dn")
      );
      if (resets.length > 0) {
        const r = resets[resets.length - 1]; // самый свежий
        const st = walk.state[r.bar];
        if (st) {
          cycle.triggerRange = captureRange(st);
          cycle.resetType    = r.type;
          cycle.resetTime    = cs[r.bar].t;
          cycle.state        = "WAIT_MID";
          cycleLog(cycle, "transition", `WAIT_BREAK → WAIT_MID (${r.type}, mid=${cycle.triggerRange.mid.toFixed(6)})`);
        }
      }
      break;
    }
    case "WAIT_MID": {
      // Railway не поддерживает limit-ордера, поэтому используем touch-then-market:
      // ждём когда цена коснётся mid ±толеранс → market-ордер на обе ноги через Railway
      const tr  = cycle.triggerRange;
      const tol = tr.width * (cycle.midTolerancePct || 0.05); // дефолт 5% от полу-ширины
      if (Math.abs(price - tr.mid) < tol) {
        cycleLog(cycle, "trigger", `price=${price.toFixed(6)} near mid=${tr.mid.toFixed(6)} (tol=${tol.toFixed(6)})`);
        const result = await openStraddleInternal(cycle.base, cycle.amount, cycle.token);
        if (result.status === "both_open") {
          cycle.entryUsdt  = result.entryUsdt;
          cycle.entryUsdc  = result.entryUsdc;
          cycle.entryTime  = Date.now();
          cycle.entryPrice = price;
          cycle.state      = "STRADDLE_OPEN";
          cycleLog(cycle, "entry", `Straddle открыт через Railway · long USDT@${result.entryUsdt} · short USDC@${result.entryUsdc}`);
        } else if (result.status === "partial") {
          cycle.state = "ERROR";
          cycleLog(cycle, "error", `Partial fill! ${result.raw?.warning || ''}`, result);
        } else {
          cycle.state = "ERROR";
          cycleLog(cycle, "error", `Straddle failed: ${result.status}`, result);
        }
      }
      break;
    }
    case "STRADDLE_OPEN": {
      // Ждём reset ПОСЛЕ входа — тот определит победившую ногу
      const resets = walk.events.filter(e =>
        cs[e.bar] && cs[e.bar].t >= cycle.entryTime &&
        (e.type === "reset-up" || e.type === "reset-dn")
      );
      if (resets.length > 0) {
        const r      = resets[0]; // первый сброс после входа
        const winner = r.type === "reset-up" ? "usdt" : "usdc";
        const loser  = winner === "usdt" ? "usdc" : "usdt";
        const loserQ = loser === "usdt" ? "USDT" : "USDC";

        cycleLog(cycle, "second_break", `${r.type} → закрываю лузера=${loser}, оставляю winner=${winner}`);
        // Закрываем лузера через Railway
        try { await railwayClose(cycle.base, loserQ, cycle.token); }
        catch (e) { cycleLog(cycle, "warn", `close loser failed: ${e.message}`); }

        const entry   = winner === "usdt" ? cycle.entryUsdt : cycle.entryUsdc;
        const tpFrac  = cycle.tpPct / 100;
        const tpPrice = winner === "usdt" ? entry * (1 + tpFrac) : entry * (1 - tpFrac);

        // Хард-стоп: A2 = противоположная граница triggerRange (дефолт)
        let slPrice = null;
        if (cycle.slMode === "opposite" || !cycle.slMode) {
          slPrice = winner === "usdt" ? cycle.triggerRange.lower : cycle.triggerRange.upper;
        } else if (cycle.slMode === "mid") {
          slPrice = cycle.triggerRange.mid;
        } else if (cycle.slMode === "fixed" && cycle.slPct > 0) {
          const sf = cycle.slPct / 100;
          slPrice = winner === "usdt" ? entry * (1 - sf) : entry * (1 + sf);
        }

        // Считаем % от entry для Railway /straddle-tpsl (принимает проценты, не абсолютные цены)
        const takePct = cycle.tpPct;
        let stopPct = 0;
        if (slPrice != null) {
          stopPct = winner === "usdt"
            ? ((entry - slPrice) / entry) * 100
            : ((slPrice - entry) / entry) * 100;
          stopPct = Math.abs(stopPct);
        }
        if (takePct > 0 && stopPct > 0) {
          try {
            // Railway /straddle-tpsl ставит и на лузера тоже — но его уже закрыли, TP/SL на nil-позиции безвредны
            await railwayStraddleTpsl(cycle.base, stopPct, takePct, cycle.token, {
              entryUsdt: winner === "usdt" ? entry : 0,
              entryUsdc: winner === "usdc" ? entry : 0,
            });
          } catch (e) { cycleLog(cycle, "warn", `straddle-tpsl failed: ${e.message}`); }
        }

        cycle.winnerSide = winner;
        cycle.keptEntry  = entry;
        cycle.tpPrice    = tpPrice;
        cycle.slPrice    = slPrice;
        cycle.stopPctCalc = stopPct;
        cycle.state      = "SINGLE_LEG";
        cycleLog(cycle, "leg_kept", `TP=${tpPrice.toFixed(6)} (${takePct}%) · SL=${slPrice?.toFixed(6) || "нет"} (${stopPct.toFixed(2)}%)`);
      }
      break;
    }
    case "SINGLE_LEG": {
      const winnerQ = cycle.winnerSide === "usdt" ? "USDT" : "USDC";
      // Позиция ещё жива? (может TP или хард-SL сработал на бирже)
      let pos = null;
      try { pos = await railwayGetPosition(cycle.base, winnerQ); } catch { /* ignore */ }
      if (!pos) {
        cycle.state = "DONE";
        cycleLog(cycle, "exit", `Позиция ${cycle.base}${winnerQ} закрыта на бирже (TP или SL сработал)`);
        break;
      }
      // Мягкий триггер: обратный reset → закрываем через Railway
      if (cycle.softStopEnabled !== false) {
        const reverseType = cycle.winnerSide === "usdt" ? "reset-dn" : "reset-up";
        const reverses = walk.events.filter(e =>
          cs[e.bar] && cs[e.bar].t >= cycle.entryTime && e.type === reverseType
        );
        if (reverses.length > 0) {
          try {
            await railwayClose(cycle.base, winnerQ, cycle.token);
            cycle.state = "DONE";
            cycleLog(cycle, "soft_stop", `Обратный ${reverseType} → ранний выход через Railway`);
          } catch (e) {
            cycleLog(cycle, "error", `soft close failed: ${e.message}`);
          }
        }
      }
      break;
    }
  }
}

// Poll loop — каждые 30 секунд оцениваем все активные циклы
const POLL_MS = 30_000;
setInterval(async () => {
  for (const [id, cycle] of CYCLES) {
    if (["DONE", "ERROR", "STOPPED"].includes(cycle.state)) continue;
    try { await evaluateCycle(cycle); }
    catch (e) { cycleLog(cycle, "error", `poll: ${e.message}`); }
  }
}, POLL_MS);

/* ────────────────────────────────────────────────────────────────────
   HTTP-сервер
   ──────────────────────────────────────────────────────────────────── */

function cors(res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "*");
}

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on("data", c => chunks.push(c));
    req.on("end", () => {
      try {
        const s = Buffer.concat(chunks).toString("utf8");
        resolve(s ? JSON.parse(s) : {});
      } catch { resolve({}); }
    });
    req.on("error", () => resolve({}));
  });
}

const server = http.createServer(async (req, res) => {
  cors(res);
  if (req.method === "OPTIONS") { res.writeHead(204); return res.end(); }

  let u;
  try { u = new URL(req.url, `http://${req.headers.host}`); }
  catch { res.writeHead(400); return res.end("bad request"); }

  // ── /health ─────────────────────────────────────────────────────
  if (u.pathname === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({
      ok: true,
      ts: Date.now(),
      cacheSize: CACHE.size,
      exchanges: Object.keys(EXCHANGES),
      trading: "railway-proxy",
      railwayUrl: RAILWAY_URL,
      leverage: LEVERAGE,
      cycles: CYCLES.size,
    }));
  }

  // ── /straddle — прокси на Railway ──────────────────────────────
  if (u.pathname === "/straddle" && req.method === "POST") {
    const token = u.searchParams.get("token") || "";
    const data  = await readBody(req);
    if (!data.base || !data.amount) {
      res.writeHead(400); return res.end(JSON.stringify({ error: "base + amount required" }));
    }
    try {
      const j = await railwayOpenStraddle(data.base.toUpperCase(), +data.amount, token);
      res.writeHead(200, { "Content-Type": "application/json" });
      return res.end(JSON.stringify(j));
    } catch (e) {
      res.writeHead(502); return res.end(JSON.stringify({ error: "railway: " + e.message }));
    }
  }

  // ── /straddle-tpsl — прокси на Railway ─────────────────────────
  if (u.pathname === "/straddle-tpsl" && req.method === "POST") {
    const token = u.searchParams.get("token") || "";
    const data  = await readBody(req);
    if (!data.base || !data.stop_pct || !data.take_pct) {
      res.writeHead(400); return res.end(JSON.stringify({ error: "base + stop_pct + take_pct required" }));
    }
    try {
      const j = await railwayStraddleTpsl(
        data.base.toUpperCase(),
        +data.stop_pct,
        +data.take_pct,
        token,
        { entryUsdt: +data.entry_usdt || 0, entryUsdc: +data.entry_usdc || 0 }
      );
      res.writeHead(200, { "Content-Type": "application/json" });
      return res.end(JSON.stringify(j));
    } catch (e) {
      res.writeHead(502); return res.end(JSON.stringify({ error: "railway: " + e.message }));
    }
  }

  // ── /leg-close — прокси на Railway/close (для usdt/usdc/both) ──
  if (u.pathname === "/leg-close" && req.method === "POST") {
    const token = u.searchParams.get("token") || "";
    const data  = await readBody(req);
    const base  = String(data.base || "").toUpperCase();
    const which = String(data.side || "").toLowerCase();
    const quotes = [];
    if (which === "usdt" || which === "both") quotes.push("USDT");
    if (which === "usdc" || which === "both") quotes.push("USDC");
    if (!base || !quotes.length) {
      res.writeHead(400); return res.end(JSON.stringify({ error: "base + side=usdt|usdc|both required" }));
    }
    const results = {};
    for (const q of quotes) {
      try { results[base + q] = await railwayClose(base, q, token); }
      catch (e) { results[base + q] = { error: String(e.message || e) }; }
    }
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ base, results }));
  }

  // ══════════════════════════════════════════════════════════════════
  //  АВТОМАТ СТРЭДДЛА (state machine cycles)
  // ══════════════════════════════════════════════════════════════════

  // ── POST /cycle/start ──────────────────────────────────────────
  if (u.pathname === "/cycle/start" && req.method === "POST") {
    if (!u.searchParams.get("token")) {
      res.writeHead(401); return res.end(JSON.stringify({ error: "unauthorized" }));
    }
    const data = await readBody(req);
    const base = String(data.base || "").toUpperCase();
    const ex   = String(data.ex || "coinex").toLowerCase();
    const amount = +data.amount || 0;
    const tpPct  = +data.tpPct  || 1.0;
    const tf     = String(data.tf || "15");
    if (!base || amount <= 0) {
      res.writeHead(400);
      return res.end(JSON.stringify({ error: "base + amount required" }));
    }
    // Один активный цикл на (base, ex)
    for (const c of CYCLES.values()) {
      if (c.base === base && c.ex === ex && !["DONE", "ERROR", "STOPPED"].includes(c.state)) {
        res.writeHead(409);
        return res.end(JSON.stringify({ error: "уже активен цикл на " + base + "/" + ex, id: c.id }));
      }
    }
    const cycle = {
      id: newCycleId(base, ex),
      base, ex, tf,
      amount, tpPct,
      slMode:  data.slMode || "opposite",
      slPct:   +data.slPct || 0,
      midTolerancePct: +data.midTolerancePct || 0.05,
      softStopEnabled: data.softStopEnabled !== false,
      token: u.searchParams.get("token"),
      state: "WAIT_BREAK",
      createdAt: Date.now(),
      updatedAt: Date.now(),
      events: [],
    };

    // ── ADOPT: проверяем открытые позиции и подхватываем ──
    // Данные с Railway; ошибка/недоступность = стартуем в WAIT_BREAK как раньше
    let posUsdt = null, posUsdc = null;
    try {
      [posUsdt, posUsdc] = await Promise.all([
        railwayGetPosition(base, "USDT").catch(() => null),
        railwayGetPosition(base, "USDC").catch(() => null),
      ]);
    } catch { /* ignore, fall through to WAIT_BREAK */ }

    const eUsdt = posUsdt ? +(posUsdt.avg_entry_price || posUsdt.entry_price || posUsdt.open_price || 0) : 0;
    const eUsdc = posUsdc ? +(posUsdc.avg_entry_price || posUsdc.entry_price || posUsdc.open_price || 0) : 0;
    const hasUsdt = eUsdt > 0;
    const hasUsdc = eUsdc > 0;

    // Захватываем текущий ренж — понадобится и для STRADDLE_OPEN (закрытие лузера), и для UI
    let currentRange = null;
    if (hasUsdt || hasUsdc) {
      try {
        const cs = await getCandles(base, tf, 500);
        if (cs && cs.length >= 305) {
          const walk = walkRange(cs);
          if (walk.range) currentRange = { ...walk.range };
        }
      } catch { /* без ренжа адаптируем — просто SL/soft-stop будут ограничены */ }
    }

    if (hasUsdt && hasUsdc) {
      // ADOPT: обе ноги → STRADDLE_OPEN
      cycle.entryUsdt  = eUsdt;
      cycle.entryUsdc  = eUsdc;
      cycle.entryTime  = Date.now(); // события трекаются с СЕЙЧАС, не с оригинального открытия
      cycle.entryPrice = (eUsdt + eUsdc) / 2;
      cycle.triggerRange = currentRange;
      cycle.state = "STRADDLE_OPEN";
      cycle.adopted = "both_legs";
      cycleLog(cycle, "adopt",
        `Подхвачен стрэддл: LONG USDT@${eUsdt} · SHORT USDC@${eUsdc}. Жду ⦿ чтобы закрыть лузера.`);
    } else if (hasUsdt || hasUsdc) {
      // ADOPT: одна нога → SINGLE_LEG (сохраняем существующие TP/SL с биржи)
      const winner  = hasUsdt ? "usdt" : "usdc";
      const winPos  = hasUsdt ? posUsdt : posUsdc;
      cycle.winnerSide = winner;
      cycle.keptEntry  = hasUsdt ? eUsdt : eUsdc;
      cycle.entryTime  = Date.now();
      cycle.entryUsdt  = hasUsdt ? eUsdt : 0;
      cycle.entryUsdc  = hasUsdc ? eUsdc : 0;
      cycle.tpPrice    = +(winPos.take_profit_price || 0) || null;
      cycle.slPrice    = +(winPos.stop_loss_price   || 0) || null;
      cycle.triggerRange = currentRange;
      cycle.state = "SINGLE_LEG";
      cycle.adopted = "single_leg";
      cycleLog(cycle, "adopt",
        `Подхвачена одна нога: ${winner.toUpperCase()}@${cycle.keptEntry} · TP=${cycle.tpPrice || '?'} · SL=${cycle.slPrice || '?'}. Слежу за soft-stop.`);
    } else {
      cycleLog(cycle, "start", `Автомат запущен · TF=${tf} · amount=${amount} · TP=${tpPct}% · SL=${cycle.slMode}. Позиций нет — жду ⦿.`);
    }

    CYCLES.set(cycle.id, cycle);
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ok: true, cycle, adopted: cycle.adopted || null }));
  }

  // ── POST /cycle/adopt-all — скан всех монет и adopt существующих ──
  // Body: {bases: ["XRP","BTC",...], tpPct, slMode?, softStop?, tf?, amount?}
  // Для каждой монеты из bases: опрашивает Railway, если найдены открытые
  // позиции (обе или одна) — создаёт цикл в STRADDLE_OPEN или SINGLE_LEG.
  // Дубли (активный цикл уже есть) — пропускает.
  if (u.pathname === "/cycle/adopt-all" && req.method === "POST") {
    if (!u.searchParams.get("token")) {
      res.writeHead(401); return res.end(JSON.stringify({ error: "unauthorized" }));
    }
    const data   = await readBody(req);
    const bases  = Array.isArray(data.bases) ? data.bases.map(b => String(b).toUpperCase()) : [];
    const tf     = String(data.tf || "15");
    const tpPct  = +data.tpPct || 1.0;
    const amount = +data.amount || 0;
    const slMode = data.slMode || "opposite";
    const softStopEnabled = data.softStopEnabled !== false;
    const token  = u.searchParams.get("token");

    if (!bases.length) {
      res.writeHead(400);
      return res.end(JSON.stringify({ error: "bases: [] required" }));
    }

    const results = [];
    for (const base of bases) {
      // Пропускаем если активный цикл уже есть
      let already = null;
      for (const c of CYCLES.values()) {
        if (c.base === base && c.ex === "coinex" && !["DONE","ERROR","STOPPED"].includes(c.state)) {
          already = c; break;
        }
      }
      if (already) {
        results.push({ base, skipped: true, reason: "уже активен цикл", id: already.id, state: already.state });
        continue;
      }

      // Читаем позиции
      let posUsdt = null, posUsdc = null;
      try {
        [posUsdt, posUsdc] = await Promise.all([
          railwayGetPosition(base, "USDT").catch(() => null),
          railwayGetPosition(base, "USDC").catch(() => null),
        ]);
      } catch {}
      const eUsdt = posUsdt ? +(posUsdt.avg_entry_price || posUsdt.entry_price || posUsdt.open_price || 0) : 0;
      const eUsdc = posUsdc ? +(posUsdc.avg_entry_price || posUsdc.entry_price || posUsdc.open_price || 0) : 0;
      if (!eUsdt && !eUsdc) {
        results.push({ base, skipped: true, reason: "нет позиций" });
        continue;
      }

      // Ренж для triggerRange
      let currentRange = null;
      try {
        const cs = await getCandles(base, tf, 500);
        if (cs && cs.length >= 305) {
          const walk = walkRange(cs);
          if (walk.range) currentRange = { ...walk.range };
        }
      } catch {}

      const cycle = {
        id: newCycleId(base, "coinex"),
        base, ex: "coinex", tf,
        amount: amount || (+posUsdt?.open_interest || +posUsdc?.open_interest || 0),
        tpPct, slMode,
        softStopEnabled,
        token,
        state: "WAIT_BREAK",
        createdAt: Date.now(),
        updatedAt: Date.now(),
        events: [],
        triggerRange: currentRange,
        entryTime: Date.now(),
      };

      if (eUsdt > 0 && eUsdc > 0) {
        cycle.entryUsdt  = eUsdt;
        cycle.entryUsdc  = eUsdc;
        cycle.entryPrice = (eUsdt + eUsdc) / 2;
        cycle.state      = "STRADDLE_OPEN";
        cycle.adopted    = "both_legs";
        cycleLog(cycle, "adopt", `Bulk-adopt стрэддл: LONG USDT@${eUsdt} · SHORT USDC@${eUsdc}`);
      } else {
        const winner = eUsdt > 0 ? "usdt" : "usdc";
        const winPos = eUsdt > 0 ? posUsdt : posUsdc;
        cycle.winnerSide = winner;
        cycle.keptEntry  = eUsdt > 0 ? eUsdt : eUsdc;
        cycle.entryUsdt  = eUsdt > 0 ? eUsdt : 0;
        cycle.entryUsdc  = eUsdc > 0 ? eUsdc : 0;
        cycle.tpPrice    = +(winPos.take_profit_price || 0) || null;
        cycle.slPrice    = +(winPos.stop_loss_price   || 0) || null;
        cycle.state      = "SINGLE_LEG";
        cycle.adopted    = "single_leg";
        cycleLog(cycle, "adopt", `Bulk-adopt одна нога: ${winner.toUpperCase()}@${cycle.keptEntry}`);
      }

      CYCLES.set(cycle.id, cycle);
      results.push({
        base, adopted: cycle.adopted, id: cycle.id, state: cycle.state,
        entry_usdt: eUsdt || null, entry_usdc: eUsdc || null,
      });
    }

    const adoptedCount = results.filter(r => r.adopted).length;
    const skippedCount = results.filter(r => r.skipped).length;
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({
      ok: true,
      total: bases.length,
      adopted: adoptedCount,
      skipped: skippedCount,
      results,
    }));
  }

  // ── GET /cycles ────────────────────────────────────────────────
  if (u.pathname === "/cycles") {
    const list = [...CYCLES.values()].sort((a, b) => b.createdAt - a.createdAt);
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ts: Date.now(), cycles: list }));
  }

  // ── GET /cycle?id=… ────────────────────────────────────────────
  if (u.pathname === "/cycle") {
    const id = u.searchParams.get("id");
    const cycle = CYCLES.get(id);
    if (!cycle) { res.writeHead(404); return res.end('{"error":"not found"}'); }
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ cycle }));
  }

  // ── POST /cycle/stop ───────────────────────────────────────────
  // Останавливает цикл. Если closePositions=true — закрывает позиции если есть
  if (u.pathname === "/cycle/stop" && req.method === "POST") {
    if (!u.searchParams.get("token")) {
      res.writeHead(401); return res.end(JSON.stringify({ error: "unauthorized" }));
    }
    const data = await readBody(req);
    const cycle = CYCLES.get(data.id);
    if (!cycle) { res.writeHead(404); return res.end('{"error":"cycle not found"}'); }
    const closeAll = data.closePositions !== false;
    const results = { closes: {} };
    const useToken = data.token || cycle.token;
    if (closeAll) {
      for (const q of ["USDT", "USDC"]) {
        try { results.closes[cycle.base + q] = await railwayClose(cycle.base, q, useToken); }
        catch (e) { results.closes[cycle.base + q] = { error: String(e.message || e) }; }
      }
    }
    cycle.state = "STOPPED";
    cycleLog(cycle, "stop", `Manual stop · closePositions=${closeAll}`, results);
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ok: true, cycle, results }));
  }

  // ── POST /cycle/update-tp ─ обновить TP на активной ноге ────────
  if (u.pathname === "/cycle/update-tp" && req.method === "POST") {
    if (!u.searchParams.get("token")) {
      res.writeHead(401); return res.end(JSON.stringify({ error: "unauthorized" }));
    }
    const data = await readBody(req);
    const cycle = CYCLES.get(data.id);
    if (!cycle) { res.writeHead(404); return res.end('{"error":"cycle not found"}'); }
    const newTp = +data.tpPct;
    if (!(newTp > 0)) { res.writeHead(400); return res.end('{"error":"tpPct required"}'); }
    cycle.tpPct = newTp;
    let result = null;
    if (cycle.state === "SINGLE_LEG" && cycle.winnerSide && cycle.keptEntry) {
      const useToken = data.token || cycle.token;
      const entry    = cycle.keptEntry;
      const tf       = newTp / 100;
      const tpPrice  = cycle.winnerSide === "usdt" ? entry * (1 + tf) : entry * (1 - tf);
      // Railway ставит /straddle-tpsl на обе ноги. Лузер уже закрыт — TP на nil-позиции безвреден
      const stopPct  = cycle.stopPctCalc || 0.5; // если есть текущий SL — сохраним, иначе 0.5%
      try {
        result = await railwayStraddleTpsl(cycle.base, stopPct, newTp, useToken, {
          entryUsdt: cycle.winnerSide === "usdt" ? entry : 0,
          entryUsdc: cycle.winnerSide === "usdc" ? entry : 0,
        });
        cycle.tpPrice = tpPrice;
        cycleLog(cycle, "tp_update", `New TP=${newTp}% → ${tpPrice.toFixed(6)}`, result);
      } catch (e) {
        cycleLog(cycle, "warn", `setTP failed: ${e.message}`);
      }
    } else {
      cycleLog(cycle, "tp_update", `New TP=${newTp}% (сохранён, будет применён при закрытии лузера)`);
    }
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ok: true, cycle, result }));
  }

  // ── POST /cycle/close ─ ручное закрытие ноги/обеих ─────────────
  if (u.pathname === "/cycle/close" && req.method === "POST") {
    if (!u.searchParams.get("token")) {
      res.writeHead(401); return res.end(JSON.stringify({ error: "unauthorized" }));
    }
    const data  = await readBody(req);
    const cycle = CYCLES.get(data.id);
    if (!cycle) { res.writeHead(404); return res.end('{"error":"cycle not found"}'); }
    const which = String(data.side || "both").toLowerCase();
    const useToken = data.token || cycle.token;
    const quotes = [];
    if (which === "usdt" || which === "both") quotes.push("USDT");
    if (which === "usdc" || which === "both") quotes.push("USDC");
    const results = {};
    for (const q of quotes) {
      try { results[cycle.base + q] = await railwayClose(cycle.base, q, useToken); }
      catch (e) { results[cycle.base + q] = { error: String(e.message || e) }; }
    }
    if (which === "both") {
      cycle.state = "DONE";
      cycleLog(cycle, "manual_close", "Both legs closed manually via Railway", results);
    } else {
      cycleLog(cycle, "manual_close", `${which} leg closed manually via Railway`, results);
    }
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ok: true, cycle, results }));
  }

  // ── /positions?base=XRP — читаем с Railway (публичный GET) ──────
  if (u.pathname === "/positions") {
    const base = (u.searchParams.get("base") || "").toUpperCase();
    if (!base) { res.writeHead(400); return res.end('{"error":"base required"}'); }
    try {
      const [pu, pc] = await Promise.all([
        railwayGetPosition(base, "USDT").catch(e => ({ error: String(e.message || e) })),
        railwayGetPosition(base, "USDC").catch(e => ({ error: String(e.message || e) })),
      ]);
      res.writeHead(200, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ base, long_usdt: pu, short_usdc: pc, ts: Date.now() }));
    } catch (e) {
      res.writeHead(500);
      return res.end(JSON.stringify({ error: String(e.message || e) }));
    }
  }

  // ── /funding-multi?bases=XRP,BTC,ETH — вся таблица ──────────────
  if (u.pathname === "/funding-multi") {
    const bases = (u.searchParams.get("bases") || "XRP,BTC,ETH,SOL,DOGE")
      .toUpperCase().split(",").map(s => s.trim()).filter(Boolean);
    try {
      const data = await scanFundingMulti(bases);
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      return res.end(JSON.stringify({ ts: Date.now(), bases, data }));
    } catch (e) {
      res.writeHead(500);
      return res.end(JSON.stringify({ error: String(e.message || e) }));
    }
  }

  // ── /funding-one?base=XRP&ex=bybit&quote=USDT — одна точка ──────
  // Для отладки: посмотреть что именно возвращает биржа
  if (u.pathname === "/funding-one") {
    const base  = (u.searchParams.get("base")  || "XRP").toUpperCase();
    const ex    = (u.searchParams.get("ex")    || "bybit").toLowerCase();
    const quote = (u.searchParams.get("quote") || "USDT").toUpperCase();
    if (!EXCHANGES[ex]) {
      res.writeHead(400);
      return res.end(JSON.stringify({ error: `unknown exchange: ${ex}` }));
    }
    const v = await getFunding(ex, base, quote);
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
    return res.end(JSON.stringify({ ex, base, quote, ...v, ts: Date.now() }));
  }

  // ── /candles?base=XRP&interval=15&limit=300 ─────────────────────
  if (u.pathname === "/candles") {
    const base = (u.searchParams.get("base") || "").toUpperCase();
    const interval = u.searchParams.get("interval") || "15";
    const limit = Math.min(1000, +u.searchParams.get("limit") || 300);
    if (!base) { res.writeHead(400); return res.end('{"error":"base required"}'); }
    const cs = await getCandles(base, interval, limit);
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
    return res.end(JSON.stringify({ base, interval, candles: cs || [], count: cs?.length || 0 }));
  }

  // ── /candles-multi?bases=XRP,BTC&interval=15 — параллельно ──────
  if (u.pathname === "/candles-multi") {
    const bases = (u.searchParams.get("bases") || "")
      .toUpperCase().split(",").map(s => s.trim()).filter(Boolean);
    const interval = u.searchParams.get("interval") || "15";
    const limit = Math.min(1000, +u.searchParams.get("limit") || 300);
    if (!bases.length) { res.writeHead(400); return res.end('{"error":"bases required"}'); }
    const pairs = await Promise.all(bases.map(async b => [b, await getCandles(b, interval, limit)]));
    const out = {};
    for (const [b, cs] of pairs) out[b] = cs || [];
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
    return res.end(JSON.stringify({ ts: Date.now(), interval, bases, data: out }));
  }

  // ── / и /hedge-v2.html — отдаём фронт ───────────────────────────
  if (u.pathname === "/" || u.pathname === "/hedge-v2.html") {
    try {
      const html = fs.readFileSync(HTML_FILE, "utf8");
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      return res.end(html);
    } catch {
      res.writeHead(404);
      return res.end("hedge-v2.html not found — положи файл рядом с relay-v2.js");
    }
  }

  res.writeHead(404);
  res.end("not found");
});

/* ────────────────────────────────────────────────────────────────────
   Старт
   ──────────────────────────────────────────────────────────────────── */

if (typeof fetch !== "function") {
  console.error("\n  ✖ Нужен Node.js 22+ (нет нативного fetch).");
  console.error("    Проверьте:  node -v   и обновите с https://nodejs.org\n");
  process.exit(1);
}

server.listen(PORT, HOST, () => {
  console.log("");
  console.log("  ✅  Hedge-Range v2 relay запущен");
  console.log("  ─────────────────────────────────────────────────────────────");
  console.log("  Веб:              http://" + HOST + ":" + PORT + "/");
  console.log("  Health:           http://" + HOST + ":" + PORT + "/health");
  console.log("  Таблица перекоса: http://" + HOST + ":" + PORT + "/funding-multi?bases=XRP,BTC,ETH,SOL");
  console.log("  Одна точка:       http://" + HOST + ":" + PORT + "/funding-one?base=XRP&ex=bybit&quote=USDT");
  console.log("  Свечи Bybit:      http://" + HOST + ":" + PORT + "/candles?base=XRP&interval=15&limit=300");
  console.log("  Свечи multi:      http://" + HOST + ":" + PORT + "/candles-multi?bases=XRP,BTC&interval=15");
  console.log("");
  console.log("  Биржи:      " + Object.keys(EXCHANGES).join(", "));
  console.log("  Кэш TTL:    " + (CACHE_TTL_MS / 1000) + "s");
  console.log("");
  // Прогреваем Railway чтобы первый запрос юзера не ловил холодный старт
  console.log("  🔥 Прогреваю Railway (может занять до 45с если сервер спал)…");
  railwayHealthCheck().then(hc => {
    if (hc.ok) console.log("  ✅ Railway готов: " + hc.server);
    else console.log("  ⚠ Railway пока не отвечает (" + hc.error + "). Попробуй /positions через минуту.");
  });
  console.log("");
  console.log("  🌐 Торговля через Railway: " + RAILWAY_URL);
  console.log("     Токен WEBHOOK_TOKEN передаётся из фронта (localStorage.hedgeV2Token)");
  console.log("     Прокси-эндпоинты (заворачивают на Railway):");
  console.log("       POST /straddle?token=…      → Railway /straddle");
  console.log("       POST /straddle-tpsl?token=… → Railway /straddle-tpsl");
  console.log("       POST /leg-close?token=…     → Railway /close (per leg)");
  console.log("       GET  /positions?base=XRP    → Railway /position/{sym}");
  console.log("");
  console.log("  🤖 АВТОМАТ (state-machine стрэддла, крутится локально):");
  console.log("     POST /cycle/start?token=…    — запустить автомат");
  console.log("     GET  /cycles                  — список всех циклов");
  console.log("     GET  /cycle?id=…              — один цикл");
  console.log("     POST /cycle/stop?token=…      — остановить + закрыть позиции");
  console.log("     POST /cycle/update-tp?token=… — изменить TP на живой ноге");
  console.log("     POST /cycle/close?token=…     — ручное закрытие ноги");
  console.log("     Poll: " + (POLL_MS/1000) + "s · Плечо: " + LEVERAGE + "× (реально на Railway)");
  console.log("");
  console.log("  Стоп: Ctrl + C");
  console.log("");
});
