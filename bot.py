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
FIRST_ENTRY_PCT = 0.30
ADD_ENTRY_PCT = 0.20
ADD_THRESHOLD_PCT = 0.015

OKX_FEE_RATE = 0.0005
TOTAL_FEE = OKX_FEE_RATE * 2
MIN_NET_PROFIT_PCT = 0.015
SL_RATIO = 0.5

MIN_VOLUME_USD = 2_000_000
MIN_MOMENTUM_PCT = 1.0
MAX_MOMENTUM_PCT = 8.0
SCAN_INTERVAL = 300
MONITOR_INTERVAL = 10
MARGIN_GROWTH_THRESHOLD = 2.0
MARGIN_GROWTH_PCT = 0.50

active_trade = None
stats = {"wins": 0, "losses": 0, "pnl": 0.0, "fees": 0.0}
base_capital = 0.0
current_margin = 0.0
last_double_at = 0.0

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

def calc_tp_sl(signal, price, high24h, low24h, margin):
    tp_pct = TOTAL_FEE + MIN_NET_PROFIT_PCT
    sl_pct = tp_pct * SL_RATIO
    if signal == "LONG":
        room = (high24h - price) / price
        if room < tp_pct:
            return None, None
        return round(price * (1 + tp_pct), 6), round(price * (1 - sl_pct), 6)
    else:
        room = (price - low24h) / price
        if room < tp_pct:
            return None, None
        return round(price * (1 - tp_pct), 6), round(price * (1 + sl_pct), 6)

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
        body = json.dumps({"instId": inst_id, "lever": str(LEVERAGE), "mgnMode": "isolated", "posSide": pos_side})
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
        body = json.dumps({"instId": inst_id, "tdMode": "isolated", "side": side, "posSide": pos_side, "ordType": "market", "sz": str(size), "clOrdId": f"bot{int(time.time())}"})
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
        body = json.dumps({"instId": inst_id, "mgnMode": "isolated", "posSide": pos_side})
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
                result.append({"instId": inst_id, "price": price, "high24h": high24h, "low24h": low24h, "change_24h": change_24h, "vol_usd": vol_usd})
            return result
    except:
        return []

async def get_candles(session, inst_id, bar="15m", limit=60):
    try:
        path = f"/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            candles = list(reversed(data.get("data", [])))
            return [float(c[4]) for c in candles], [float(c[5]) for c in candles]
    except:
        return [], []

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

def get_momentum(closes):
    if len(closes) < 6:
        return 0
    avg_r = sum(closes[-3:]) / 3
    avg_p = sum(closes[-6:-3]) / 3
    return (avg_r - avg_p) / avg_p * 100 if avg_p > 0 else 0

def get_trend(closes_1h):
    if len(closes_1h) < 20:
        return "SIDEWAYS"
    e10 = ema(closes_1h, 10)
    e20 = ema(closes_1h, 20)
    price = closes_1h[-1]
    if price > e10 > e20:
        return "UPTREND"
    elif price < e10 < e20:
        return "DOWNTREND"
    return "SIDEWAYS"

def analyze(closes_15m, volumes, trend, high24h, low24h):
    if len(closes_15m) < 30:
        return "WAIT", {}
    price = closes_15m[-1]
    r = rsi(closes_15m)
    e9 = ema(closes_15m, 9)
    e21 = ema(closes_15m, 21)
    momentum = get_momentum(closes_15m)
    vol_ok = sum(volumes[-3:]) / 3 > sum(volumes[-8:-3]) / 5 * 1.2 if len(volumes) >= 8 else False
    range_24h = high24h - low24h
    price_pos = (price - low24h) / range_24h * 100 if range_24h > 0 else 50
    info = {"rsi": r, "momentum": momentum, "price_pos": price_pos, "vol_ok": vol_ok, "trend": trend, "high24h": high24h, "low24h": low24h, "price": price}
    if trend == "UPTREND":
        if 30 < r < 60 and e9 > e21 and momentum > 0.3 and price_pos < 75 and vol_ok:
            return "LONG", info
    elif trend == "DOWNTREND":
        if 40 < r < 70 and e9 < e21 and momentum < -0.3 and price_pos > 25 and vol_ok:
            return "SHORT", info
    return "WAIT", info

async def scan_best_coin(session):
    tickers = await get_all_futures(session)
    candidates = [t for t in tickers if t["vol_usd"] >= MIN_VOLUME_USD and MIN_MOMENTUM_PCT <= abs(t["change_24h"]) <= MAX_MOMENTUM_PCT]
    candidates.sort(key=lambda x: x["vol_usd"], reverse=True)
    for coin in candidates[:30]:
        inst_id = coin["instId"]
        closes_1h, _ = await get_candles(session, inst_id, "1H", 30)
        if not closes_1h:
            continue
        trend = get_trend(closes_1h)
        if trend == "SIDEWAYS":
            await asyncio.sleep(0.1)
            continue
        closes_15m, volumes_15m = await get_candles(session, inst_id, "15m", 60)
        if not closes_15m:
            continue
        signal, info = analyze(closes_15m, volumes_15m, trend, coin["high24h"], coin["low24h"])
        if signal == "WAIT":
            await asyncio.sleep(0.1)
            continue
        tp, sl = calc_tp_sl(signal, coin["price"], coin["high24h"], coin["low24h"], current_margin * FIRST_ENTRY_PCT)
        if tp is None:
            await asyncio.sleep(0.1)
            continue
        log(f"✅ {inst_id} | {signal} | Trend:{trend} | RSI:{info['rsi']:.1f}")
        return inst_id, signal, coin["price"], info, tp, sl
        await asyncio.sleep(0.2)
    return None, "WAIT", 0, {}, 0, 0

async def open_trade(session, inst_id, signal, price, info, tp, sl):
    global active_trade
    margin_1 = current_margin * FIRST_ENTRY_PCT
    position_size = margin_1 * LEVERAGE
    fee_est = position_size * TOTAL_FEE
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
    active_trade = {"inst_id": inst_id, "signal": signal, "pos_side": pos_side, "entry_price": price, "total_margin": margin_1, "size": size, "tp_price": tp, "sl_price": sl, "entry_time": time.time(), "added": False, "closed": False, "fee_est": fee_est}
    coin_name = inst_id.replace("-SWAP", "")
    tp_pct = abs(tp - price) / price * 100
    sl_pct = abs(sl - price) / price * 100
    net_tp = (tp_pct / 100 - TOTAL_FEE) * position_size
    net_sl = (sl_pct / 100 + TOTAL_FEE) * position_size
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
        f"🏦 Fees: ~${fee_est:.4f}\n"
        f"📊 Ratio: {net_tp/net_sl:.1f}:1\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📈 Trend: {info.get('trend')} | RSI: {info.get('rsi', 0):.1f}\n"
        f"⚡ Momentum: {info.get('momentum', 0):+.2f}%\n"
        f"📍 Position 24h: {info.get('price_pos', 0):.0f}%\n"
        f"📊 High: {info.get('high24h')} | Low: {info.get('low24h')}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Margin: ${margin_1:.2f} | {LEVERAGE}x\n"
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
            change = (price - trade["entry_price"]) / trade["entry_price"] if trade["signal"] == "LONG" else (trade["entry_price"] - price) / trade["entry_price"]
            if not trade["added"] and change >= ADD_THRESHOLD_PCT:
                margin_2 = current_margin * ADD_ENTRY_PCT
                position_2 = margin_2 * LEVERAGE
                min_sz, ct_val = await get_instrument_info(session, trade["inst_id"])
                size_2 = max(round(position_2 / (price * ct_val), 0), min_sz)
                if not DRY_RUN:
                    side = "buy" if trade["signal"] == "LONG" else "sell"
                    await place_order(session, trade["inst_id"], side, trade["pos_side"], int(size_2))
                trade["total_margin"] += margin_2
                trade["fee_est"] += position_2 * TOTAL_FEE
                trade["added"] = True
                mode = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"
                await send_telegram(session, f"➕ IMEONGEZA!\n📊 {coin_name}\n💲 Bei: {price}\n💰 +${margin_2:.2f}\n📈 {change*100:+.2f}%\n⚡ {mode}")
            tp_hit = (trade["signal"] == "LONG" and price >= trade["tp_price"]) or (trade["signal"] == "SHORT" and price <= trade["tp_price"])
            sl_hit = (trade["signal"] == "LONG" and price <= trade["sl_price"]) or (trade["signal"] == "SHORT" and price >= trade["sl_price"])
            if tp_hit or sl_hit:
                if not DRY_RUN:
                    await close_position(session, trade["inst_id"], trade["pos_side"])
                trade["closed"] = True
                gross_pnl = change * trade["total_margin"] * LEVERAGE
                fees = trade["fee_est"]
                net_pnl = gross_pnl - fees
                hold = (time.time() - trade["entry_time"]) / 60
                if net_pnl > 0:
                    stats["wins"] += 1
                    emoji = "💰 TAKE PROFIT!"
                else:
                    stats["losses"] += 1
                    emoji = "🛑 STOP LOSS!"
                stats["pnl"] += net_pnl
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
                    f"💵 Gross: {gross_pnl:+.4f} USDT\n"
                    f"🏦 Fees: -{fees:.4f} USDT\n"
                    f"✅ Net PnL: {net_pnl:+.4f} USDT\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📊 Jumla Net: {stats['pnl']:+.4f} USDT\n"
                    f"🏦 Fees zote: ${stats['fees']:.4f}\n"
                    f"💰 Balance: ${balance:.4f} USDT\n"
                    f"🏆 Win Rate: {win_rate:.0f}% | ✅{stats['wins']} ❌{stats['losses']}\n"
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
    if balance > 0 and balance >= last_double_at * MARGIN_GROWTH_THRESHOLD:
        old = current_margin
        current_margin = current_margin * (1 + MARGIN_GROWTH_PCT)
        last_double_at = balance
        await send_telegram(session, f"📈 MARGIN IMEONGEZWA!\n💰 Balance: ${balance:.4f}\n📊 Zamani: ${old:.2f}\n📊 Mpya: ${current_margin:.2f}\n🎯 Itaongezwa tena: ${balance * MARGIN_GROWTH_THRESHOLD:.2f}")

async def scanner_loop(session):
    global active_trade
    await asyncio.sleep(5)
    while True:
        try:
            if active_trade and not active_trade["closed"]:
                log(f"⏳ Trade wazi: {active_trade['inst_id']}")
                await asyncio.sleep(SCAN_INTERVAL)
                continue
            log("🔍 Inascan...")
            result = await scan_best_coin(session)
            if result[0] is None:
                log("Hakuna signal — inasubiri...")
                await asyncio.sleep(SCAN_INTERVAL)
                continue
            inst_id, signal, price, info, tp, sl = result
            await open_trade(session, inst_id, signal, price, info, tp, sl)
            await asyncio.sleep(SCAN_INTERVAL)
        except Exception as e:
            log(f"Scanner error: {e}")
            await asyncio.sleep(30)

async def main():
    global base_capital, current_margin, last_double_at
    async with aiohttp.ClientSession() as session:
        await set_position_mode(session)
        balance = await get_futures_balance(session)
        base_capital = balance
        current_margin = balance
        last_double_at = balance
        tp_pct = (TOTAL_FEE + MIN_NET_PROFIT_PCT) * 100
        sl_pct = tp_pct * SL_RATIO
        net_tp = tp_pct - TOTAL_FEE * 100
        net_sl = sl_pct + TOTAL_FEE * 100
        mode = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"
        msg = (
            f"⚡ OKX FUTURES — FEE AWARE!\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 Balance: ${balance:.4f} USDT\n"
            f"🔢 Leverage: {LEVERAGE}x | Isolated\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🏦 OKX Fee: {TOTAL_FEE*100:.2f}% per trade\n"
            f"🎯 TP gross: +{tp_pct:.2f}% → Net: +{net_tp:.2f}%\n"
            f"🛑 SL gross: -{sl_pct:.2f}% → Net: -{net_sl:.2f}%\n"
            f"📊 Ratio TP:SL = {net_tp/net_sl:.1f}:1\n"
            f"━━━━━━━━━━━━━━━\n"
            f"⚡ Mode: {mode}"
        )
        log(msg)
        await send_telegram(session, msg)
        await asyncio.gather(scanner_loop(session), monitor_trade(session))

if __name__ == "__main__":
    asyncio.run(main())
