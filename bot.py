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
DRY_RUN = True

SYMBOL_1 = "BTC-USDT-SWAP"
SYMBOL_2 = "ETH-USDT-SWAP"

LEVERAGE = 10
MARGIN_PER_TRADE = 5
TAKE_PROFIT_PCT = 0.025
STOP_LOSS_PCT = 0.012
MAX_HOLD_MINUTES = 45
CHECK_INTERVAL = 90

active_positions = {}
trade_count = 0
total_pnl = 0.0

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

async def get_candles(session, symbol, bar="1H", limit=100):
    try:
        path = f"/api/v5/market/candles?instId={symbol}&bar={bar}&limit={limit}"
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

async def get_candles_15m(session, symbol, limit=50):
    try:
        path = f"/api/v5/market/candles?instId={symbol}&bar=15m&limit={limit}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            candles = list(reversed(data.get("data", [])))
            closes = [float(c[4]) for c in candles]
            volumes = [float(c[5]) for c in candles]
            return closes, volumes
    except:
        return [], []

async def get_price(session, symbol):
    try:
        path = f"/api/v5/market/ticker?instId={symbol}"
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

def detect_trend(closes_1h):
    if len(closes_1h) < 50:
        return "SIDEWAYS"

    ema20 = ema(closes_1h, 20)
    ema50 = ema(closes_1h, 50)
    price = closes_1h[-1]

    recent_highs = [max(closes_1h[i-5:i]) for i in range(5, len(closes_1h))]
    recent_lows = [min(closes_1h[i-5:i]) for i in range(5, len(closes_1h))]

    higher_highs = recent_highs[-1] > recent_highs[-3] > recent_highs[-5]
    higher_lows = recent_lows[-1] > recent_lows[-3] > recent_lows[-5]
    lower_highs = recent_highs[-1] < recent_highs[-3] < recent_highs[-5]
    lower_lows = recent_lows[-1] < recent_lows[-3] < recent_lows[-5]

    if price > ema20 > ema50 and higher_highs and higher_lows:
        return "UPTREND"
    elif price < ema20 < ema50 and lower_highs and lower_lows:
        return "DOWNTREND"
    else:
        return "SIDEWAYS"

def get_signal(closes_15m, volumes_15m, trend):
    if len(closes_15m) < 30:
        return "WAIT"

    r = rsi(closes_15m, 14)
    ema9 = ema(closes_15m, 9)
    ema21 = ema(closes_15m, 21)
    price = closes_15m[-1]

    vol_surge = sum(volumes_15m[-3:]) / 3 > sum(volumes_15m[-10:-3]) / 7 * 1.3

    if trend == "UPTREND":
        if r > 30 and r < 60 and ema9 > ema21 and price > ema9 and vol_surge:
            return "LONG"

    elif trend == "DOWNTREND":
        if r < 70 and r > 40 and ema9 < ema21 and price < ema9 and vol_surge:
            return "SHORT"

    elif trend == "SIDEWAYS":
        if r < 30 and price < ema21 * 0.998 and vol_surge:
            return "LONG"
        elif r > 70 and price > ema21 * 1.002 and vol_surge:
            return "SHORT"

    return "WAIT"

async def open_trade(session, symbol, signal, price, trend):
    global trade_count

    if signal == "LONG":
        tp = price * (1 + TAKE_PROFIT_PCT)
        sl = price * (1 - STOP_LOSS_PCT)
    else:
        tp = price * (1 - TAKE_PROFIT_PCT)
        sl = price * (1 + STOP_LOSS_PCT)

    trade_id = f"{symbol}_{int(time.time())}"
    active_positions[trade_id] = {
        "symbol": symbol,
        "side": signal,
        "entry": price,
        "tp": tp,
        "sl": sl,
        "entry_time": time.time(),
        "trend": trend,
        "closed": False
    }

    trade_count += 1

    msg = (
        f"{'🟢' if signal == 'LONG' else '🔴'} {signal} #{trade_count}\n"
        f"📊 {symbol}\n"
        f"💲 Entry: ${price:,.2f}\n"
        f"🎯 TP: ${tp:,.2f}\n"
        f"🛑 SL: ${sl:,.2f}\n"
        f"📈 Trend: {trend}\n"
        f"💰 ${MARGIN_PER_TRADE} x {LEVERAGE}x\n"
        f"🧪 SIM"
    )
    log(f"Trade #{trade_count}: {signal} {symbol} @ {price:.2f}")
    await send_telegram(session, msg)

async def close_trade(session, trade_id, price, reason):
    global total_pnl
    pos = active_positions[trade_id]
    pos["closed"] = True

    if pos["side"] == "LONG":
        pnl_pct = (price - pos["entry"]) / pos["entry"]
    else:
        pnl_pct = (pos["entry"] - price) / pos["entry"]

    pnl = pnl_pct * MARGIN_PER_TRADE * LEVERAGE
    total_pnl += pnl
    hold = (time.time() - pos["entry_time"]) / 60

    emoji = "💰" if pnl > 0 else "🛑"
    reason_map = {"TP": "TAKE PROFIT ✅", "SL": "STOP LOSS ❌", "TIME": "MUDA ⏱️"}

    msg = (
        f"{emoji} {reason_map.get(reason, reason)}\n"
        f"📊 {pos['symbol']}\n"
        f"{'🟢 LONG' if pos['side'] == 'LONG' else '🔴 SHORT'}\n"
        f"💲 Entry: ${pos['entry']:,.2f}\n"
        f"💲 Exit: ${price:,.2f}\n"
        f"⏱️ {hold:.0f} dakika\n"
        f"💵 PnL: {'+' if pnl >= 0 else ''}{pnl:.2f} USDT\n"
        f"📊 Jumla: ${total_pnl:.2f} USDT\n"
        f"🧪 SIM"
    )
    log(f"Closed: {pos['side']} {pos['symbol']} PnL={pnl:.2f}")
    await send_telegram(session, msg)

async def monitor(session):
    while True:
        try:
            for tid, pos in list(active_positions.items()):
                if pos["closed"]:
                    continue

                price = await get_price(session, pos["symbol"])
                if not price:
                    continue

                elapsed = (time.time() - pos["entry_time"]) / 60
                tp_hit = (pos["side"] == "LONG" and price >= pos["tp"]) or \
                         (pos["side"] == "SHORT" and price <= pos["tp"])
                sl_hit = (pos["side"] == "LONG" and price <= pos["sl"]) or \
                         (pos["side"] == "SHORT" and price >= pos["sl"])

                if tp_hit:
                    await close_trade(session, tid, price, "TP")
                elif sl_hit:
                    await close_trade(session, tid, price, "SL")
                elif elapsed >= MAX_HOLD_MINUTES:
                    await close_trade(session, tid, price, "TIME")

            await asyncio.sleep(15)
        except Exception as e:
            log(f"Monitor error: {e}")
            await asyncio.sleep(15)

async def run(session):
    while True:
        try:
            for symbol in [SYMBOL_1, SYMBOL_2]:
                open_count = sum(
                    1 for p in active_positions.values()
                    if symbol in p["symbol"] and not p["closed"]
                )
                if open_count >= 1:
                    await asyncio.sleep(3)
                    continue

                closes_1h, _, _, _ = await get_candles(session, symbol, "1H", 100)
                if not closes_1h:
                    continue

                trend = detect_trend(closes_1h)

                if trend == "SIDEWAYS":
                    log(f"⏳ {symbol} — SIDEWAYS, inasubiri trend...")
                    await asyncio.sleep(3)
                    continue

                closes_15m, volumes_15m = await get_candles_15m(session, symbol, 50)
                if not closes_15m:
                    continue

                signal = get_signal(closes_15m, volumes_15m, trend)
                price = closes_15m[-1]

                if signal != "WAIT":
                    await open_trade(session, symbol, signal, price, trend)
                else:
                    log(f"⏳ {symbol} — {trend} | Signal: WAIT")

                await asyncio.sleep(3)

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            log(f"Run error: {e}")
            await asyncio.sleep(10)

async def main():
    async with aiohttp.ClientSession() as session:
        msg = (
            f"⚡ OKX SMART FUTURES BOT\n"
            f"📊 BTC + ETH\n"
            f"🧠 Trend Detection: 1H\n"
            f"⏱️ Signal: 15m\n"
            f"🎯 TP: {TAKE_PROFIT_PCT*100:.1f}% | SL: {STOP_LOSS_PCT*100:.1f}%\n"
            f"🔢 {LEVERAGE}x | ${MARGIN_PER_TRADE}/trade\n"
            f"🧪 SIMULATION"
        )
        log(msg)
        await send_telegram(session, msg)

        await asyncio.gather(run(session), monitor(session))

if __name__ == "__main__":
    asyncio.run(main())
