import asyncio
import aiohttp
import hmac
import hashlib
import base64
import time
from datetime import datetime, timezone

OKX_API_KEY = "a0457663-9fdc-4787-b27e-5b7b7f34e99b"
OKX_SECRET = "B803CF81AB7DCFD262399F893D755497"
OKX_PASSPHRASE = "Futuresbot2026."

TELEGRAM_TOKEN = "8787267026:AAHjMfzdg9JwVxdCo6pnoiNq2o1xvU2pC30"
TELEGRAM_CHAT_ID = "7010983039"

OKX_BASE = "https://www.okx.com"

CAPITAL = 20.0
LEVERAGE = 5
FIRST_ENTRY_PCT = 0.30
ADD_ENTRY_PCT = 0.20
TP_PCT = 0.04
SL_PCT = 0.025
ADD_THRESHOLD_PCT = 0.015

MIN_VOLUME_USD = 1_000_000
MIN_CHANGE_PCT = 2.0
MAX_CHANGE_PCT = 15.0
SCAN_INTERVAL = 300
MONITOR_INTERVAL = 15

active_trade = None
stats = {"wins": 0, "losses": 0, "pnl": 0.0}

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

async def get_candles(session, inst_id, bar="15m", limit=100):
    try:
        path = f"/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            candles = list(reversed(data.get("data", [])))
            closes = [float(c[4]) for c in candles]
            highs = [float(c[2]) for c in candles]
            lows = [float(c[3]) for c in candles]
            volumes = [float(c[5]) for c in candles]
            return closes, highs, lows, volumes
    except:
        return [], [], [], []

async def get_price(session, inst_id):
    try:
        path = f"/api/v5/market/ticker?instId={inst_id}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            return float(data.get("data", [{}])[0].get("last", 0))
    except:
        return 0.0

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
                sod = float(t.get("sodUtc8", price))
                change_pct = abs((price - sod) / sod * 100) if sod > 0 else 0
                result.append({
                    "instId": inst_id,
                    "price": price,
                    "change_pct": change_pct,
                    "vol_usd": vol_usd
                })
            return result
    except:
        return []

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

def get_trend_1h(closes_1h):
    if len(closes_1h) < 50:
        return "SIDEWAYS"
    e20 = ema(closes_1h, 20)
    e50 = ema(closes_1h, 50)
    price = closes_1h[-1]
    last_5 = closes_1h[-5:]
    higher_highs = last_5[-1] > last_5[0]
    lower_lows = last_5[-1] < last_5[0]
    if price > e20 > e50 and higher_highs:
        return "UPTREND"
    elif price < e20 < e50 and lower_lows:
        return "DOWNTREND"
    return "SIDEWAYS"

def analyze_15m(closes, volumes, trend):
    if len(closes) < 50:
        return "WAIT", {}

    r = rsi(closes, 14)
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    price = closes[-1]

    prev_e9 = ema(closes[:-1], 9)
    prev_e21 = ema(closes[:-1], 21)
    ema_crossed_up = prev_e9 <= prev_e21 and e9 > e21
    ema_crossed_down = prev_e9 >= prev_e21 and e9 < e21

    vol_surge = sum(volumes[-3:]) / 3 > sum(volumes[-10:-3]) / 7 * 1.3 if len(volumes) >= 10 else False

    info = {
        "rsi": r,
        "e9": e9,
        "e21": e21,
        "price": price,
        "trend": trend,
        "vol_surge": vol_surge,
        "ema_crossed_up": ema_crossed_up,
        "ema_crossed_down": ema_crossed_down
    }

    if trend == "UPTREND":
        if (
            25 < r < 55 and
            e9 > e21 and
            price > e9 and
            vol_surge
        ):
            return "LONG", info

    elif trend == "DOWNTREND":
        if (
            45 < r < 75 and
            e9 < e21 and
            price < e9 and
            vol_surge
        ):
            return "SHORT", info

    elif trend == "SIDEWAYS":
        if r < 30 and ema_crossed_up and vol_surge:
            return "LONG", info
        elif r > 70 and ema_crossed_down and vol_surge:
            return "SHORT", info

    return "WAIT", info

async def scan_best_coin(session):
    log("🔍 Inascan coins...")
    tickers = await get_all_futures(session)

    candidates = [
        t for t in tickers
        if t["vol_usd"] >= MIN_VOLUME_USD
        and MIN_CHANGE_PCT <= t["change_pct"] <= MAX_CHANGE_PCT
    ]
    candidates.sort(key=lambda x: x["vol_usd"], reverse=True)

    best_coin = None
    best_signal = "WAIT"
    best_info = {}
    best_price = 0

    for coin in candidates[:25]:
        inst_id = coin["instId"]

        closes_1h, _, _, _ = await get_candles(session, inst_id, "1H", 60)
        if not closes_1h:
            continue

        trend = get_trend_1h(closes_1h)

        if trend == "SIDEWAYS":
            log(f"⏭️ {inst_id} — SIDEWAYS, inaruka...")
            await asyncio.sleep(0.2)
            continue

        closes_15m, _, _, volumes_15m = await get_candles(session, inst_id, "15m", 100)
        if not closes_15m:
            continue

        signal, info = analyze_15m(closes_15m, volumes_15m, trend)

        if signal != "WAIT":
            log(f"✅ {inst_id} | {signal} | Trend: {trend} | RSI: {info.get('rsi', 0):.1f}")
            best_coin = inst_id
            best_signal = signal
            best_info = info
            best_price = closes_15m[-1]
            break

        await asyncio.sleep(0.3)

    return best_coin, best_signal, best_price, best_info

async def open_trade(session, inst_id, signal, price, info):
    global active_trade

    margin_1 = CAPITAL * FIRST_ENTRY_PCT
    size_1 = (margin_1 * LEVERAGE) / price

    if signal == "LONG":
        tp_price = round(price * (1 + TP_PCT), 6)
        sl_price = round(price * (1 - SL_PCT), 6)
    else:
        tp_price = round(price * (1 - TP_PCT), 6)
        sl_price = round(price * (1 + SL_PCT), 6)

    active_trade = {
        "inst_id": inst_id,
        "signal": signal,
        "entry_price": price,
        "current_size": size_1,
        "total_margin": margin_1,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "entry_time": time.time(),
        "added": False,
        "closed": False
    }

    coin_name = inst_id.replace("-SWAP", "")
    trend = info.get("trend", "")
    r = info.get("rsi", 0)
    vol = "✅" if info.get("vol_surge") else "❌"

    msg = (
        f"🎯 IMEINGIA TRADE!\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 {coin_name}\n"
        f"{'🟢 LONG' if signal == 'LONG' else '🔴 SHORT'}\n"
        f"💲 Entry: {price}\n"
        f"🎯 TP: {tp_price}\n"
        f"🛑 SL: {sl_price}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📈 Trend 1H: {trend}\n"
        f"📊 RSI: {r:.1f}\n"
        f"📦 Volume: {vol}\n"
        f"💰 Margin: ${margin_1:.2f}\n"
        f"🔢 Leverage: {LEVERAGE}x\n"
        f"🧪 SIMULATION"
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
                margin_2 = CAPITAL * ADD_ENTRY_PCT
                size_2 = (margin_2 * LEVERAGE) / price
                trade["current_size"] += size_2
                trade["total_margin"] += margin_2
                trade["added"] = True
                msg = (
                    f"➕ IMEONGEZA POSITION!\n"
                    f"📊 {coin_name}\n"
                    f"💲 Bei: {price}\n"
                    f"💰 Imeongeza: ${margin_2:.2f}\n"
                    f"📈 Faida: {change*100:+.2f}%\n"
                    f"🧪 SIMULATION"
                )
                await send_telegram(session, msg)

            tp_hit = (trade["signal"] == "LONG" and price >= trade["tp_price"]) or \
                     (trade["signal"] == "SHORT" and price <= trade["tp_price"])
            sl_hit = (trade["signal"] == "LONG" and price <= trade["sl_price"]) or \
                     (trade["signal"] == "SHORT" and price >= trade["sl_price"])

            if tp_hit or sl_hit:
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
                    f"🏆 Win Rate: {win_rate:.0f}%\n"
                    f"✅ {stats['wins']} | ❌ {stats['losses']}\n"
                    f"🧪 SIMULATION"
                )
                log(msg)
                await send_telegram(session, msg)
                active_trade = None

            await asyncio.sleep(MONITOR_INTERVAL)

        except Exception as e:
            log(f"Monitor error: {e}")
            await asyncio.sleep(MONITOR_INTERVAL)

async def scanner_loop(session):
    global active_trade
    await asyncio.sleep(5)

    while True:
        try:
            if active_trade and not active_trade["closed"]:
                log(f"⏳ Trade wazi: {active_trade['inst_id']}")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            await send_telegram(session, "🔍 Inatafuta coin nzuri...")
            best_coin, signal, price, info = await scan_best_coin(session)

            if not best_coin:
                await send_telegram(session, "⏳ Hakuna signal nzuri sasa.\n🔄 Dakika 5...")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            await open_trade(session, best_coin, signal, price, info)
            await asyncio.sleep(SCAN_INTERVAL)

        except Exception as e:
            log(f"Scanner error: {e}")
            await asyncio.sleep(30)

async def main():
    async with aiohttp.ClientSession() as session:
        msg = (
            f"⚡ OKX DUAL FUTURES BOT V2!\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 Capital: ${CAPITAL}\n"
            f"🔢 Leverage: {LEVERAGE}x\n"
            f"🎯 TP: {TP_PCT*100:.0f}% | SL: {SL_PCT*100:.1f}%\n"
            f"🧠 Trend: 1H | Signal: 15m\n"
            f"📊 Entry: {FIRST_ENTRY_PCT*100:.0f}% + {ADD_ENTRY_PCT*100:.0f}%\n"
            f"🧪 SIMULATION"
        )
        log(msg)
        await send_telegram(session, msg)
        await asyncio.gather(scanner_loop(session), monitor_trade(session))

if __name__ == "__main__":
    asyncio.run(main())
