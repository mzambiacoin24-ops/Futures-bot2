import asyncio
import aiohttp
import hmac
import hashlib
import base64
import json
import time
from datetime import datetime, timezone

OKX_API_KEY = "a0457663-9fdc-4787-b27e-5b7b7f34e99b"
OKX_SECRET = "B803CF81AB7DCFD262399F893D755497"
OKX_PASSPHRASE = "Futuresbot2026."
TELEGRAM_TOKEN = "8787267026:AAHjMfzdg9JwVxdCo6pnoiNq2o1xvU2pC30"
TELEGRAM_CHAT_ID = "7010983039"
OKX_BASE = "https://www.okx.com"

DRY_RUN = False
LEVERAGE = 5
FIRST_ENTRY_PCT = 0.10
ADD_ENTRY_PCT = 0.10
ADD_THRESHOLD_PCT = 0.02

OKX_FEE = 0.0005 * 2
TP_PCT = 0.03
SL_PCT = 0.025

# Punguza overtrade — subiri dakika 90 kati ya trades
MIN_WAIT_BETWEEN_TRADES = 90 * 60
MAX_TRADES_PER_DAY = 15

MIN_SCORE = 6
MIN_VOLUME_USD = 2_000_000
MIN_CHANGE_PCT = 1.5
MAX_CHANGE_PCT = 10.0
SCAN_INTERVAL = 600
MONITOR_INTERVAL = 15

MARGIN_GROWTH_X = 2.0
MARGIN_GROWTH_PCT = 0.50

active_trade = None
stats = {"wins": 0, "losses": 0, "pnl": 0.0, "fees": 0.0}
current_margin = 0.0
last_double_at = 0.0
last_trade_time = 0
trades_today = 0
last_trade_day = ""

def log(msg):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}")

async def send_telegram(session, msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        await session.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except Exception as e:
        log(f"Telegram error: {e}")

def get_headers(method, path, body=""):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    msg = ts + method.upper() + path + body
    sig = base64.b64encode(hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    return {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }

async def set_position_mode(session):
    try:
        path = "/api/v5/account/set-position-mode"
        body = json.dumps({"posMode": "long_short_mode"})
        headers = get_headers("POST", path, body)
        async with session.post(OKX_BASE + path, headers=headers, data=body) as r:
            data = await r.json()
            log(f"Position mode: {data.get('msg', 'OK')}")
    except Exception as e:
        log(f"Position mode error: {e}")

async def get_instrument_info(session, inst_id):
    try:
        path = f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            inst = data.get("data", [{}])[0]
            return float(inst.get("minSz", 1)), float(inst.get("ctVal", 1))
    except:
        return 1, 1

async def get_futures_balance(session):
    try:
        path = "/api/v5/account/balance?ccy=USDT"
        headers = get_headers("GET", path)
        async with session.get(OKX_BASE + path, headers=headers) as r:
            data = await r.json()
            for d in data.get("data", [{}])[0].get("details", []):
                if d.get("ccy") == "USDT":
                    return float(d.get("availBal", 0))
        return 0.0
    except:
        return 0.0

async def set_leverage(session, inst_id, pos_side):
    try:
        path = "/api/v5/account/set-leverage"
        body = json.dumps({
            "instId": inst_id,
            "lever": str(LEVERAGE),
            "mgnMode": "isolated",
            "posSide": pos_side
        })
        headers = get_headers("POST", path, body)
        async with session.post(OKX_BASE + path, headers=headers, data=body) as r:
            await r.json()
    except:
        pass

async def place_order(session, inst_id, side, pos_side, size):
    if DRY_RUN:
        return {"ordId": f"sim_{int(time.time())}"}
    try:
        path = "/api/v5/trade/order"
        body = json.dumps({
            "instId": inst_id,
            "tdMode": "isolated",
            "side": side,
            "posSide": pos_side,
            "ordType": "market",
            "sz": str(size),
            "clOrdId": f"bot{int(time.time())}"
        })
        headers = get_headers("POST", path, body)
        async with session.post(OKX_BASE + path, headers=headers, data=body) as r:
            data = await r.json()
            if data.get("code") == "0":
                result = data.get("data", [{}])[0]
                if result.get("sCode") == "0":
                    return result
                log(f"Order failed: {result.get('sMsg')}")
            else:
                log(f"Order error: {data.get('msg')}")
            return None
    except Exception as e:
        log(f"Order error: {e}")
        return None

async def close_position(session, inst_id, pos_side):
    if DRY_RUN:
        return True
    try:
        path = "/api/v5/trade/close-position"
        body = json.dumps({
            "instId": inst_id,
            "mgnMode": "isolated",
            "posSide": pos_side
        })
        headers = get_headers("POST", path, body)
        async with session.post(OKX_BASE + path, headers=headers, data=body) as r:
            data = await r.json()
            return data.get("code") == "0"
    except:
        return False

async def get_all_futures(session):
    try:
        path = "/api/v5/market/tickers?instType=SWAP"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            result = []
            for t in data.get("data", []):
                inst_id = t.get("instId", "")
                if not inst_id.endswith("-USDT-SWAP"):
                    continue
                price = float(t.get("last", 0))
                vol_usd = float(t.get("volCcy24h", 0)) * price
                high24h = float(t.get("high24h", 0))
                low24h = float(t.get("low24h", 0))
                sod = float(t.get("sodUtc8", price))
                change_24h = ((price - sod) / sod * 100) if sod > 0 else 0
                result.append({
                    "instId": inst_id,
                    "price": price,
                    "high24h": high24h,
                    "low24h": low24h,
                    "change_24h": change_24h,
                    "vol_usd": vol_usd
                })
            return result
    except:
        return []

async def get_candles(session, inst_id, bar="15m", limit=220):
    try:
        path = f"/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            candles = list(reversed(data.get("data", [])))
            opens   = [float(c[1]) for c in candles]
            highs   = [float(c[2]) for c in candles]
            lows    = [float(c[3]) for c in candles]
            closes  = [float(c[4]) for c in candles]
            volumes = [float(c[5]) for c in candles]
            return opens, highs, lows, closes, volumes
    except:
        return [], [], [], [], []

async def get_price(session, inst_id):
    try:
        path = f"/api/v5/market/ticker?instId={inst_id}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            return float(data.get("data", [{}])[0].get("last", 0))
    except:
        return 0.0

def ema(closes, period):
    if len(closes) < period:
        return closes[-1] if closes else 0
    k = 2 / (period + 1)
    e = sum(closes[:period]) / period
    for p in closes[period:]:
        e = p * k + e * (1 - k)
    return e

def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100
    return 100 - (100 / (1 + ag / al))

def is_fake_breakout(opens, highs, lows, closes, signal):
    if len(closes) < 3:
        return False
    body = abs(closes[-1] - opens[-1])
    cr = highs[-1] - lows[-1]
    if cr == 0:
        return False
    upper_wick = highs[-1] - max(closes[-1], opens[-1])
    lower_wick = min(closes[-1], opens[-1]) - lows[-1]
    if signal == "LONG" and upper_wick > body * 1.5:
        return True
    if signal == "SHORT" and lower_wick > body * 1.5:
        return True
    return False

def near_key_level(highs, lows, price, signal):
    if len(highs) < 10:
        return False
    rh = max(highs[-15:])
    rl = min(lows[-15:])
    if signal == "LONG" and (rh - price) / price < 0.004:
        return True
    if signal == "SHORT" and (price - rl) / price < 0.004:
        return True
    return False

def analyze(opens, highs, lows, closes, volumes, high24h, low24h):
    if len(closes) < 210:
        return "WAIT", {}

    price = closes[-1]
    r = rsi(closes, 14)
    e50  = ema(closes, 50)
    e200 = ema(closes, 200)

    range_24h = high24h - low24h
    price_pos = (price - low24h) / range_24h * 100 if range_24h > 0 else 50

    vol_ok = False
    if len(volumes) >= 8:
        vol_ok = sum(volumes[-3:]) / 3 > sum(volumes[-8:-3]) / 5 * 1.3

    candle_body = abs(closes[-1] - opens[-1])
    candle_range = highs[-1] - lows[-1]
    strong_candle = (candle_body / candle_range > 0.45) if candle_range > 0 else False

    momentum = (closes[-1] - closes[-4]) / closes[-4] * 100 if closes[-4] > 0 else 0

    # Angalia candle 3 zilizopita zinaelekea upande gani
    consecutive_up = all(closes[-i] > closes[-i-1] for i in range(1, 3))
    consecutive_down = all(closes[-i] < closes[-i-1] for i in range(1, 3))

    info = {
        "rsi": r, "e50": e50, "e200": e200,
        "price_pos": price_pos, "vol_ok": vol_ok,
        "strong_candle": strong_candle, "momentum": momentum,
        "high24h": high24h, "low24h": low24h, "price": price
    }

    # LONG: RSI > 50, EMA50 > EMA200
    if r > 50 and e50 > e200:
        score = sum([
            r > 50,
            e50 > e200,
            price > e50,
            price_pos < 55,
            vol_ok,
            strong_candle,
            momentum > 0.15,
            consecutive_up,
        ])
        info["score"] = score
        if score >= MIN_SCORE:
            if is_fake_breakout(opens, highs, lows, closes, "LONG"):
                return "WAIT", info
            if near_key_level(highs, lows, price, "LONG"):
                return "WAIT", info
            return "LONG", info

    # SHORT: RSI < 50, EMA50 < EMA200
    if r < 50 and e50 < e200:
        score = sum([
            r < 50,
            e50 < e200,
            price < e50,
            price_pos > 45,
            vol_ok,
            strong_candle,
            momentum < -0.15,
            consecutive_down,
        ])
        info["score"] = score
        if score >= MIN_SCORE:
            if is_fake_breakout(opens, highs, lows, closes, "SHORT"):
                return "WAIT", info
            if near_key_level(highs, lows, price, "SHORT"):
                return "WAIT", info
            return "SHORT", info

    return "WAIT", info

async def scan_best_coin(session):
    tickers = await get_all_futures(session)
    candidates = [
        t for t in tickers
        if t["vol_usd"] >= MIN_VOLUME_USD
        and MIN_CHANGE_PCT <= abs(t["change_24h"]) <= MAX_CHANGE_PCT
    ]
    candidates.sort(key=lambda x: x["vol_usd"], reverse=True)

    best_candidates = []

    for coin in candidates[:40]:
        inst_id = coin["instId"]
        opens, highs, lows, closes, volumes = await get_candles(session, inst_id, "15m", 220)
        if len(closes) < 210:
            await asyncio.sleep(0.1)
            continue

        signal, info = analyze(opens, highs, lows, closes, volumes, coin["high24h"], coin["low24h"])
        if signal == "WAIT":
            await asyncio.sleep(0.1)
            continue

        score = info.get("score", 0)
        log(f"✅ {inst_id} | {signal} | RSI:{info['rsi']:.1f} | Score:{score}/8")
        best_candidates.append((score, inst_id, signal, coin["price"], info))
        await asyncio.sleep(0.2)

    if not best_candidates:
        return None, "WAIT", 0, {}

    best_candidates.sort(key=lambda x: x[0], reverse=True)
    score, inst_id, signal, price, info = best_candidates[0]
    return inst_id, signal, price, info

async def open_trade(session, inst_id, signal, price, info):
    global active_trade, last_trade_time, trades_today, last_trade_day

    margin_1 = current_margin * FIRST_ENTRY_PCT
    position_size = margin_1 * LEVERAGE
    fee_est = position_size * OKX_FEE
    min_sz, ct_val = await get_instrument_info(session, inst_id)
    size = max(round(position_size / (price * ct_val), 0), min_sz)
    pos_side = "long" if signal == "LONG" else "short"
    side = "buy" if signal == "LONG" else "sell"

    await set_leverage(session, inst_id, pos_side)

    if not DRY_RUN:
        result = await place_order(session, inst_id, side, pos_side, int(size))
        if not result:
            log("Order imeshindwa")
            return

    if signal == "LONG":
        tp = round(price * (1 + TP_PCT), 6)
        sl = round(price * (1 - SL_PCT), 6)
    else:
        tp = round(price * (1 - TP_PCT), 6)
        sl = round(price * (1 + SL_PCT), 6)

    active_trade = {
        "inst_id": inst_id,
        "signal": signal,
        "pos_side": pos_side,
        "entry_price": price,
        "total_margin": margin_1,
        "position_size": position_size,
        "tp_price": tp,
        "sl_price": sl,
        "entry_time": time.time(),
        "added": False,
        "closed": False,
        "fee_est": fee_est
    }

    last_trade_time = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    if last_trade_day != today:
        trades_today = 0
        last_trade_day = today
    trades_today += 1

    coin_name = inst_id.replace("-SWAP", "")
    score = info.get("score", 0)
    tp_pct = abs(tp - price) / price * 100
    sl_pct = abs(sl - price) / price * 100
    net_tp = (TP_PCT - OKX_FEE) * position_size
    net_sl = (SL_PCT + OKX_FEE) * position_size
    mode = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"

    msg = (
        f"🎯 IMEINGIA TRADE!\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 {coin_name}\n"
        f"{'🟢 LONG' if signal == 'LONG' else '🔴 SHORT'}\n"
        f"💲 Entry: {price}\n"
        f"🎯 TP: {tp} (+{tp_pct:.2f}%)\n"
        f"🛑 SL: {sl} (-{sl_pct:.2f}%)\n"
        f"━━━━━━━━━━━━━━━\n"
        f"✅ Net faida ikiTP: +${net_tp:.4f}\n"
        f"❌ Net hasara ikiSL: -${net_sl:.4f}\n"
        f"📊 Ratio: {net_tp/net_sl:.1f}:1\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 RSI: {info.get('rsi', 0):.1f}\n"
        f"📈 EMA50: {info.get('e50', 0):.4f}\n"
        f"📉 EMA200: {info.get('e200', 0):.4f}\n"
        f"📍 Position 24h: {info.get('price_pos', 0):.0f}%\n"
        f"📦 Volume: {'✅' if info.get('vol_ok') else '❌'}\n"
        f"🕯️ Candle: {'✅' if info.get('strong_candle') else '❌'}\n"
        f"⚡ Momentum: {info.get('momentum', 0):+.2f}%\n"
        f"🎯 Score: {score}/8\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Margin: ${margin_1:.2f} (10%)\n"
        f"🔢 Leverage: {LEVERAGE}x\n"
        f"📅 Trades leo: {trades_today}/{MAX_TRADES_PER_DAY}\n"
        f"⚡ {mode}"
    )
    log(msg)
    await send_telegram(session, msg)

async def monitor_trade(session):
    global active_trade, stats

    while True:
        try:
            if not active_trade or active_trade["closed"]:
                await asyncio.sleep(MONITOR_INTERVAL)
                continue

            trade = active_trade
            price = await get_price(session, trade["inst_id"])
            if not price:
                await asyncio.sleep(MONITOR_INTERVAL)
                continue

            coin_name = trade["inst_id"].replace("-SWAP", "")

            if trade["signal"] == "LONG":
                change = (price - trade["entry_price"]) / trade["entry_price"]
            else:
                change = (trade["entry_price"] - price) / trade["entry_price"]

            if not trade["added"] and change >= ADD_THRESHOLD_PCT:
                margin_2 = current_margin * ADD_ENTRY_PCT
                position_2 = margin_2 * LEVERAGE
                min_sz, ct_val = await get_instrument_info(session, trade["inst_id"])
                size_2 = max(round(position_2 / (price * ct_val), 0), min_sz)
                if not DRY_RUN:
                    side = "buy" if trade["signal"] == "LONG" else "sell"
                    await place_order(session, trade["inst_id"], side, trade["pos_side"], int(size_2))
                trade["total_margin"] += margin_2
                trade["position_size"] += position_2
                trade["fee_est"] += position_2 * OKX_FEE
                trade["added"] = True
                mode = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"
                await send_telegram(session,
                    f"➕ IMEONGEZA!\n"
                    f"📊 {coin_name}\n"
                    f"💲 Bei: {price}\n"
                    f"💰 +${margin_2:.2f} (10% zaidi)\n"
                    f"📈 {change*100:+.2f}%\n"
                    f"⚡ {mode}"
                )

            tp_hit = (trade["signal"] == "LONG" and price >= trade["tp_price"]) or \
                     (trade["signal"] == "SHORT" and price <= trade["tp_price"])
            sl_hit = (trade["signal"] == "LONG" and price <= trade["sl_price"]) or \
                     (trade["signal"] == "SHORT" and price >= trade["sl_price"])

            if tp_hit or sl_hit:
                if not DRY_RUN:
                    await close_position(session, trade["inst_id"], trade["pos_side"])

                trade["closed"] = True
                gross = change * trade["total_margin"] * LEVERAGE
                fees = trade["fee_est"]
                net = gross - fees
                hold = (time.time() - trade["entry_time"]) / 60

                if net > 0:
                    stats["wins"] += 1
                    emoji = "💰 TAKE PROFIT!"
                else:
                    stats["losses"] += 1
                    emoji = "🛑 STOP LOSS!"

                stats["pnl"] += net
                stats["fees"] += fees
                win_rate = (stats["wins"] / max(stats["wins"] + stats["losses"], 1)) * 100
                balance = await get_futures_balance(session)
                mode = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"

                msg = (
                    f"{emoji}\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📊 {coin_name}\n"
                    f"{'🟢 LONG' if trade['signal'] == 'LONG' else '🔴 SHORT'}\n"
                    f"💲 Entry: {trade['entry_price']}\n"
                    f"💲 Exit: {price}\n"
                    f"⏱️ {hold:.0f} dakika\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"💵 Gross: {gross:+.4f}\n"
                    f"🏦 Fees: -{fees:.4f}\n"
                    f"✅ Net PnL: {net:+.4f} USDT\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📊 Jumla: {stats['pnl']:+.4f} USDT\n"
                    f"🏦 Fees zote: ${stats['fees']:.4f}\n"
                    f"💰 Balance: ${balance:.4f} USDT\n"
                    f"🏆 Win Rate: {win_rate:.0f}% | ✅{stats['wins']} ❌{stats['losses']}\n"
                    f"📅 Trades leo: {trades_today}/{MAX_TRADES_PER_DAY}\n"
                    f"⚡ {mode}"
                )
                log(msg)
                await send_telegram(session, msg)
                active_trade = None
                await check_margin_growth(session)

            await asyncio.sleep(MONITOR_INTERVAL)

        except Exception as e:
            log(f"Monitor error: {e}")
            await asyncio.sleep(MONITOR_INTERVAL)

async def check_margin_growth(session):
    global current_margin, last_double_at
    balance = await get_futures_balance(session)
    if balance > 0 and balance >= last_double_at * MARGIN_GROWTH_X:
        old = current_margin
        current_margin = current_margin * (1 + MARGIN_GROWTH_PCT)
        last_double_at = balance
        await send_telegram(session,
            f"📈 MARGIN IMEONGEZWA!\n"
            f"💰 Balance: ${balance:.4f}\n"
            f"📊 ${old:.2f} → ${current_margin:.2f}\n"
            f"🎯 Itaongezwa: ${balance * MARGIN_GROWTH_X:.2f}"
        )

async def scanner_loop(session):
    global active_trade, last_trade_time, trades_today, last_trade_day

    await asyncio.sleep(5)

    while True:
        try:
            # Angalia limit ya trades kwa siku
            today = datetime.now().strftime("%Y-%m-%d")
            if last_trade_day != today:
                trades_today = 0
                last_trade_day = today

            if active_trade and not active_trade["closed"]:
                log(f"⏳ Trade wazi: {active_trade['inst_id']}")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            if trades_today >= MAX_TRADES_PER_DAY:
                log(f"🛑 Limit ya leo imefikiwa: {trades_today} trades")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            # Subiri kati ya trades
            elapsed = time.time() - last_trade_time
            if last_trade_time > 0 and elapsed < MIN_WAIT_BETWEEN_TRADES:
                wait_left = int((MIN_WAIT_BETWEEN_TRADES - elapsed) / 60)
                log(f"⏳ Inasubiri dakika {wait_left} kati ya trades...")
                await asyncio.sleep(60)
                continue

            log("🔍 Inascan...")
            inst_id, signal, price, info = await scan_best_coin(session)

            if not inst_id:
                log("Hakuna signal nzuri — inasubiri...")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            await open_trade(session, inst_id, signal, price, info)
            await asyncio.sleep(SCAN_INTERVAL)

        except Exception as e:
            log(f"Scanner error: {e}")
            await asyncio.sleep(30)

async def main():
    global current_margin, last_double_at

    async with aiohttp.ClientSession() as session:
        await set_position_mode(session)
        balance = await get_futures_balance(session)
        current_margin = balance
        last_double_at = balance

        net_tp = (TP_PCT - OKX_FEE) * 100
        net_sl = (SL_PCT + OKX_FEE) * 100
        mode = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"

        msg = (
            f"⚡ OKX FUTURES — SMART & SLOW!\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 Balance: ${balance:.4f} USDT\n"
            f"🔢 Leverage: {LEVERAGE}x | Isolated\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🎯 TP: +3% → Net: +{net_tp:.1f}%\n"
            f"🛑 SL: -2.5% → Net: -{net_sl:.1f}%\n"
            f"📊 Ratio: {net_tp/net_sl:.1f}:1\n"
            f"━━━━━━━━━━━━━━━\n"
            f"⏱️ Min kati ya trades: 90 dakika\n"
            f"📅 Max trades kwa siku: {MAX_TRADES_PER_DAY}\n"
            f"💰 Entry: 10% | Add: +10%\n"
            f"🧠 EMA 50/200 | RSI 14\n"
            f"🎯 Min Score: {MIN_SCORE}/8\n"
            f"⚡ Mode: {mode}"
        )
        log(msg)
        await send_telegram(session, msg)
        await asyncio.gather(scanner_loop(session), monitor_trade(session))

if __name__ == "__main__":
    asyncio.run(main())
