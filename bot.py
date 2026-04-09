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
TP_PCT = 0.03
SL_PCT = 0.015
ADD_THRESHOLD_PCT = 0.015

MIN_VOLUME_USD = 1_000_000
MIN_CHANGE_PCT = 2.0
MAX_CHANGE_PCT = 12.0
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

async def get_all_futures(session):
    try:
        path = "/api/v5/market/tickers?instType=SWAP"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            tickers = data.get("data", [])
            result = []
            for t in tickers:
                inst_id = t.get("instId", "")
                if not inst_id.endswith("-USDT-SWAP"):
                    continue
                vol = float(t.get("volCcy24h", 0))
                price = float(t.get("last", 0))
                change = float(t.get("sodUtc8", price))
                if change > 0:
                    change_pct = abs((price - change) / change * 100)
                else:
                    change_pct = 0
                vol_usd = vol * price
                result.append({
                    "instId": inst_id,
                    "price": price,
                    "change_pct": change_pct,
                    "vol_usd": vol_usd
                })
            return result
    except Exception as e:
        log(f"Tickers error: {e}")
        return []

async def get_candles(session, inst_id, bar="15m", limit=50):
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

def macd(closes):
    if len(closes) < 35:
        return 0, 0
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    macd_line = e12 - e26
    macd_hist = []
    for i in range(26, len(closes)):
        m = ema(closes[:i], 12) - ema(closes[:i], 26)
        macd_hist.append(m)
    if len(macd_hist) < 9:
        return macd_line, 0
    signal = ema(macd_hist, 9)
    return macd_line, signal

def analyze(closes, volumes):
    if len(closes) < 30:
        return "WAIT", 0

    r = rsi(closes)
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    m, ms = macd(closes)
    price = closes[-1]
    vol_ok = sum(volumes[-3:]) / 3 > sum(volumes[-10:-3]) / 7 * 1.2

    long_score = sum([
        r < 45,
        e9 > e21,
        price > e9,
        m > ms,
        vol_ok
    ])

    short_score = sum([
        r > 55,
        e9 < e21,
        price < e9,
        m < ms,
        vol_ok
    ])

    if long_score >= 4:
        return "LONG", long_score
    elif short_score >= 4:
        return "SHORT", short_score
    return "WAIT", 0

async def scan_best_coin(session):
    log("🔍 Inascan coins zote za OKX Futures...")

    tickers = await get_all_futures(session)

    candidates = [
        t for t in tickers
        if t["vol_usd"] >= MIN_VOLUME_USD
        and MIN_CHANGE_PCT <= t["change_pct"] <= MAX_CHANGE_PCT
    ]

    candidates.sort(key=lambda x: x["vol_usd"], reverse=True)

    best_coin = None
    best_signal = "WAIT"
    best_score = 0
    best_price = 0

    for coin in candidates[:20]:
        inst_id = coin["instId"]
        closes, volumes = await get_candles(session, inst_id)
        if not closes:
            continue

        signal, score = analyze(closes, volumes)
        if signal != "WAIT" and score > best_score:
            best_coin = inst_id
            best_signal = signal
            best_score = score
            best_price = closes[-1]
            log(f"✅ Candidate: {inst_id} | {signal} | Score: {score}/5")

        await asyncio.sleep(0.3)

    return best_coin, best_signal, best_price

async def open_trade(session, inst_id, signal, price):
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

    msg = (
        f"🎯 IMEINGIA TRADE!\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 {coin_name}\n"
        f"{'🟢 LONG' if signal == 'LONG' else '🔴 SHORT'}\n"
        f"💲 Entry: {price}\n"
        f"🎯 TP: {tp_price}\n"
        f"🛑 SL: {sl_price}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Margin: ${margin_1:.2f} ({FIRST_ENTRY_PCT*100:.0f}% ya ${CAPITAL})\n"
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
                    f"💲 Bei sasa: {price}\n"
                    f"💰 Imeongeza: ${margin_2:.2f}\n"
                    f"💰 Jumla Margin: ${trade['total_margin']:.2f}\n"
                    f"📈 Faida hadi sasa: {change*100:+.2f}%\n"
                    f"🧪 SIMULATION"
                )
                log(msg)
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
                    f"⏱️ Hold: {hold:.0f} dakika\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"💵 PnL: {'+' if pnl >= 0 else ''}{pnl:.3f} USDT\n"
                    f"📊 Jumla PnL: {stats['pnl']:+.3f} USDT\n"
                    f"🏆 Win Rate: {win_rate:.0f}%\n"
                    f"✅ {stats['wins']} Wins | ❌ {stats['losses']} Losses\n"
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
                log(f"⏳ Trade iko wazi: {active_trade['inst_id']} — inasubiri...")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            await send_telegram(session, "🔍 Inatafuta coin nzuri...")

            best_coin, signal, price = await scan_best_coin(session)

            if not best_coin:
                log("❌ Hakuna coin nzuri — itajaribu tena dakika 5...")
                await send_telegram(session, "❌ Hakuna coin nzuri sasa.\n🔄 Itajaribu tena dakika 5...")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            await open_trade(session, best_coin, signal, price)
            await asyncio.sleep(SCAN_INTERVAL)

        except Exception as e:
            log(f"Scanner error: {e}")
            await asyncio.sleep(30)

async def main():
    async with aiohttp.ClientSession() as session:
        msg = (
            f"⚡ OKX DUAL FUTURES BOT IMEANZA!\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 Capital: ${CAPITAL}\n"
            f"🔢 Leverage: {LEVERAGE}x\n"
            f"🎯 TP: {TP_PCT*100:.0f}% | SL: {SL_PCT*100:.1f}%\n"
            f"📊 Entry 1: {FIRST_ENTRY_PCT*100:.0f}% ya capital\n"
            f"📊 Entry 2: {ADD_ENTRY_PCT*100:.0f}% (ikielekea vizuri)\n"
            f"🔍 Scan kila: {SCAN_INTERVAL//60} dakika\n"
            f"🧪 SIMULATION"
        )
        log(msg)
        await send_telegram(session, msg)

        await asyncio.gather(
            scanner_loop(session),
            monitor_trade(session)
        )

if __name__ == "__main__":
    asyncio.run(main())
