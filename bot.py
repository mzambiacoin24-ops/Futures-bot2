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
TP_PCT = 0.02
SL_PCT = 0.012
ADD_THRESHOLD_PCT = 0.01

MIN_VOLUME_USD = 2_000_000
MIN_MOMENTUM_PCT = 1.0
MAX_MOMENTUM_PCT = 8.0
SCAN_INTERVAL = 300
MONITOR_INTERVAL = 10

MARGIN_GROWTH_THRESHOLD = 2.0
MARGIN_GROWTH_PCT = 0.50

active_trade = None
stats = {"wins": 0, "losses": 0, "pnl": 0.0}
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
    sig = base64.b64encode(
        hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }

async def set_position_mode(session):
    """Weka account kwenye long_short_mode ili posSide ifanye kazi"""
    try:
        path = "/api/v5/account/set-position-mode"
        body = json.dumps({"posMode": "long_short_mode"})
        headers = get_headers("POST", path, body)
        async with session.post(OKX_BASE + path, headers=headers, data=body) as r:
            data = await r.json()
            if data.get("code") == "0":
                log("✅ Position mode: long_short_mode")
            else:
                log(f"Position mode: {data.get('msg')} (inaweza tayari kuwa imewekwa)")
    except Exception as e:
        log(f"Position mode error: {e}")

async def get_instrument_info(session, inst_id):
    """Pata minimum size ya coin"""
    try:
        path = f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            inst = data.get("data", [{}])[0]
            min_sz = float(inst.get("minSz", 1))
            ct_val = float(inst.get("ctVal", 1))
            return min_sz, ct_val
    except:
        return 1, 1

async def get_futures_balance(session):
    try:
        path = "/api/v5/account/balance?ccy=USDT"
        headers = get_headers("GET", path)
        async with session.get(OKX_BASE + path, headers=headers) as r:
            data = await r.json()
            details = data.get("data", [{}])[0].get("details", [])
            for d in details:
                if d.get("ccy") == "USDT":
                    return float(d.get("availBal", 0))
        return 0.0
    except Exception as e:
        log(f"Balance error: {e}")
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
            data = await r.json()
            if data.get("code") == "0":
                log(f"✅ Leverage {LEVERAGE}x imewekwa: {inst_id} {pos_side}")
            else:
                log(f"Leverage warning: {data.get('msg')}")
    except Exception as e:
        log(f"Leverage error: {e}")

async def place_order(session, inst_id, side, pos_side, size):
    if DRY_RUN:
        log(f"[SIM] {side} {inst_id} sz={size} posSide={pos_side}")
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
                    log(f"✅ Order imefanikiwa: {result.get('ordId')}")
                    return result
                else:
                    log(f"Order failed: {result.get('sMsg')}")
                    return None
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
    except Exception as e:
        log(f"Close error: {e}")
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
                vol = float(t.get("volCcy24h", 0))
                vol_usd = vol * price
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

async def get_candles(session, inst_id, bar="15m", limit=60):
    try:
        path = f"/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            candles = list(reversed(data.get("data", [])))
            closes = [float(c[4]) for c in candles]
            volumes = [float(c[5]) for c in candles]
            return closes, volumes
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
    avg_recent = sum(closes[-3:]) / 3
    avg_prev = sum(closes[-6:-3]) / 3
    if avg_prev == 0:
        return 0
    return (avg_recent - avg_prev) / avg_prev * 100

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
    price_position = (price - low24h) / range_24h * 100 if range_24h > 0 else 50
    info = {
        "rsi": r, "momentum": momentum,
        "price_position": price_position,
        "vol_ok": vol_ok, "trend": trend,
        "high24h": high24h, "low24h": low24h, "price": price
    }
    if trend == "UPTREND":
        if 30 < r < 60 and e9 > e21 and momentum > 0.3 and price_position < 75 and vol_ok:
            return "LONG", info
    elif trend == "DOWNTREND":
        if 40 < r < 70 and e9 < e21 and momentum < -0.3 and price_position > 25 and vol_ok:
            return "SHORT", info
    return "WAIT", info

def calc_tp_sl(signal, price, high24h, low24h):
    if signal == "LONG":
        room = (high24h - price) / price
        tp_pct = min(TP_PCT, room * 0.7)
        if tp_pct < 0.008:
            return None, None
        return round(price * (1 + tp_pct), 6), round(price * (1 - SL_PCT), 6)
    else:
        room = (price - low24h) / price
        tp_pct = min(TP_PCT, room * 0.7)
        if tp_pct < 0.008:
            return None, None
        return round(price * (1 - tp_pct), 6), round(price * (1 + SL_PCT), 6)

async def scan_best_coin(session):
    tickers = await get_all_futures(session)
    candidates = [
        t for t in tickers
        if t["vol_usd"] >= MIN_VOLUME_USD
        and MIN_MOMENTUM_PCT <= abs(t["change_24h"]) <= MAX_MOMENTUM_PCT
    ]
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
        tp, sl = calc_tp_sl(signal, coin["price"], coin["high24h"], coin["low24h"])
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
    min_sz, ct_val = await get_instrument_info(session, inst_id)
    size = max(round((margin_1 * LEVERAGE) / (price * ct_val), 0), min_sz)

    pos_side = "long" if signal == "LONG" else "short"
    side = "buy" if signal == "LONG" else "sell"

    await set_leverage(session, inst_id, pos_side)

    if not DRY_RUN:
        result = await place_order(session, inst_id, side, pos_side, int(size))
        if not result:
            log("Order imeshindwa — inaruka trade hii")
            return

    active_trade = {
        "inst_id": inst_id,
        "signal": signal,
        "pos_side": pos_side,
        "entry_price": price,
        "total_margin": margin_1,
        "size": size,
        "tp_price": tp,
        "sl_price": sl,
        "entry_time": time.time(),
        "added": False,
        "closed": False
    }

    coin_name = inst_id.replace("-SWAP", "")
    tp_pct = abs(tp - price) / price * 100
    sl_pct = abs(sl - price) / price * 100
    mode = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"

    msg = (
        f"🎯 IMEINGIA TRADE!\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 {coin_name}\n"
        f"{'🟢 LONG' if signal == 'LONG' else '🔴 SHORT'}\n"
        f"💲 Entry: {price}\n"
        f"🎯 TP: {tp} (+{tp_pct:.1f}%)\n"
        f"🛑 SL: {sl} (-{sl_pct:.1f}%)\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📈 Trend 1H: {info.get('trend')}\n"
        f"📊 RSI: {info.get('rsi', 0):.1f}\n"
        f"⚡ Momentum: {info.get('momentum', 0):+.2f}%\n"
        f"📍 Position 24h: {info.get('price_position', 0):.0f}%\n"
        f"📦 Volume: {'✅' if info.get('vol_ok') else '❌'}\n"
        f"📊 High: {info.get('high24h')} | Low: {info.get('low24h')}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Margin: ${margin_1:.2f}\n"
        f"🔢 Leverage: {LEVERAGE}x\n"
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
                min_sz, ct_val = await get_instrument_info(session, trade["inst_id"])
                size_2 = max(round((margin_2 * LEVERAGE) / (price * ct_val), 0), min_sz)
                if not DRY_RUN:
                    side = "buy" if trade["signal"] == "LONG" else "sell"
                    await place_order(session, trade["inst_id"], side, trade["pos_side"], int(size_2))
                trade["total_margin"] += margin_2
                trade["added"] = True
                mode = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"
                await send_telegram(session,
                    f"➕ IMEONGEZA POSITION!\n"
                    f"📊 {coin_name}\n"
                    f"💲 Bei: {price}\n"
                    f"💰 +${margin_2:.2f}\n"
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
                pnl = change * trade["total_margin"] * LEVERAGE
                hold = (time.time() - trade["entry_time"]) / 60

                if pnl > 0:
                    stats["wins"] += 1
                    emoji = "💰 TAKE PROFIT!"
                else:
                    stats["losses"] += 1
                    emoji = "🛑 STOP LOSS!"

                stats["pnl"] += pnl
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
                    f"💵 PnL: {'+' if pnl >= 0 else ''}{pnl:.3f} USDT\n"
                    f"📊 Jumla: {stats['pnl']:+.3f} USDT\n"
                    f"💰 Balance: ${balance:.4f} USDT\n"
                    f"🏆 Win Rate: {win_rate:.0f}%\n"
                    f"✅ {stats['wins']} | ❌ {stats['losses']}\n"
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
    if balance <= 0:
        return

    if balance >= last_double_at * MARGIN_GROWTH_THRESHOLD:
        old_margin = current_margin
        current_margin = current_margin * (1 + MARGIN_GROWTH_PCT)
        last_double_at = balance

        await send_telegram(session,
            f"📈 MARGIN IMEONGEZWA!\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 Balance: ${balance:.4f} USDT\n"
            f"📊 Zamani: ${old_margin:.2f}\n"
            f"📊 Mpya: ${current_margin:.2f}\n"
            f"🎯 Itaongezwa tena: ${balance * MARGIN_GROWTH_THRESHOLD:.2f}"
        )

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
            best_coin, signal, price, info, tp, sl = result

            if not best_coin:
                log("Hakuna signal — inasubiri...")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            await open_trade(session, best_coin, signal, price, info, tp, sl)
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

        mode = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"

        msg = (
            f"⚡ OKX FUTURES BOT V3 LIVE!\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 Balance: ${balance:.4f} USDT\n"
            f"📊 Margin: ${current_margin:.4f}\n"
            f"🔢 Leverage: {LEVERAGE}x | Isolated\n"
            f"🎯 TP: hadi 2% | SL: 1.2%\n"
            f"📈 Margin itaongezwa: ${balance * MARGIN_GROWTH_THRESHOLD:.2f}\n"
            f"⚡ Mode: {mode}"
        )
        log(msg)
        await send_telegram(session, msg)
        await asyncio.gather(scanner_loop(session), monitor_trade(session))

if __name__ == "__main__":
    asyncio.run(main())
