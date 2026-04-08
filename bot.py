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

TIMEFRAME = "5m"
LEVERAGE = 10
MARGIN_PER_TRADE = 5
TAKE_PROFIT_PCT = 0.02
STOP_LOSS_PCT = 0.01
MAX_HOLD_MINUTES = 30
CHECK_INTERVAL = 60

RSI_PERIOD = 14
RSI_OVERSOLD = 40
RSI_OVERBOUGHT = 60
BB_PERIOD = 20

stats = {
    "total_trades": 0,
    "wins": 0,
    "losses": 0,
    "total_pnl": 0.0,
    "skipped": 0
}

active_positions = {}

def log(msg):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}")

async def send_telegram(session, msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        await session.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except Exception as e:
        log(f"Telegram error: {e}")

def get_okx_headers(method, path, body=""):
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    message = timestamp + method.upper() + path + body
    signature = base64.b64encode(
        hmac.new(
            OKX_SECRET.encode(),
            message.encode(),
            hashlib.sha256
        ).digest()
    ).decode()
    return {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }

async def get_candles(session, symbol, bar="5m", limit=100):
    try:
        path = f"/api/v5/market/candles?instId={symbol}&bar={bar}&limit={limit}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            candles = list(reversed(data.get("data", [])))
            closes = [float(c[4]) for c in candles]
            volumes = [float(c[5]) for c in candles]
            return closes, volumes
    except Exception as e:
        log(f"Candles error: {e}")
        return [], []

async def get_current_price(session, symbol):
    try:
        path = f"/api/v5/market/ticker?instId={symbol}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            ticker = data.get("data", [{}])[0]
            return float(ticker.get("last", 0))
    except:
        return 0.0

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))

def calculate_ema(closes, period):
    if len(closes) < period:
        return closes[-1] if closes else 0
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calculate_macd(closes):
    if len(closes) < 35:
        return 0, 0
    ema12 = calculate_ema(closes, 12)
    ema26 = calculate_ema(closes, 26)
    macd = ema12 - ema26

    macd_history = []
    for i in range(26, len(closes)):
        e12 = calculate_ema(closes[:i], 12)
        e26 = calculate_ema(closes[:i], 26)
        macd_history.append(e12 - e26)

    if len(macd_history) < 9:
        return macd, 0

    signal = calculate_ema(macd_history, 9)
    return macd, signal

def calculate_bollinger(closes, period=20):
    if len(closes) < period:
        p = closes[-1] if closes else 0
        return p, p, p
    recent = closes[-period:]
    mid = sum(recent) / period
    std = (sum((x - mid) ** 2 for x in recent) / period) ** 0.5
    return mid + 2 * std, mid, mid - 2 * std

def calculate_volume_trend(volumes):
    if len(volumes) < 10:
        return False
    return sum(volumes[-5:]) / 5 > sum(volumes[-10:-5]) / 5 * 1.1

def analyze_market(closes, volumes):
    if len(closes) < 50:
        return "WAIT", {}

    rsi = calculate_rsi(closes, RSI_PERIOD)
    macd, macd_signal = calculate_macd(closes)
    bb_upper, bb_mid, bb_lower = calculate_bollinger(closes, BB_PERIOD)
    vol_up = calculate_volume_trend(volumes)
    price = closes[-1]

    long_score = sum([
        rsi < RSI_OVERSOLD,
        macd > macd_signal,
        price < bb_lower * 1.002,
        vol_up
    ])

    short_score = sum([
        rsi > RSI_OVERBOUGHT,
        macd < macd_signal,
        price > bb_upper * 0.998,
        vol_up
    ])

    info = {
        "rsi": rsi,
        "macd": macd,
        "macd_signal": macd_signal,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "price": price,
        "vol_up": vol_up,
        "long_score": long_score,
        "short_score": short_score
    }

    if long_score >= 3:
        return "LONG", info
    elif short_score >= 3:
        return "SHORT", info
    return "WAIT", info

async def open_trade(session, symbol, signal, info):
    price = info["price"]

    if signal == "LONG":
        tp = price * (1 + TAKE_PROFIT_PCT)
        sl = price * (1 - STOP_LOSS_PCT)
        side = "BUY"
    else:
        tp = price * (1 - TAKE_PROFIT_PCT)
        sl = price * (1 + STOP_LOSS_PCT)
        side = "SELL"

    trade_id = f"{symbol}_{int(time.time())}"
    active_positions[trade_id] = {
        "symbol": symbol,
        "side": signal,
        "entry_price": price,
        "tp_price": tp,
        "sl_price": sl,
        "entry_time": time.time(),
        "margin": MARGIN_PER_TRADE,
        "closed": False
    }

    stats["total_trades"] += 1

    vol_emoji = "✅" if info["vol_up"] else "❌"
    msg = (
        f"⚡ TRADE #{stats['total_trades']} IMEFUNGULIWA!\n"
        f"📊 {symbol}\n"
        f"{'🟢 LONG' if signal == 'LONG' else '🔴 SHORT'}\n"
        f"💲 Bei: ${price:,.2f}\n"
        f"🎯 TP: ${tp:,.2f} (+{TAKE_PROFIT_PCT*100:.1f}%)\n"
        f"🛑 SL: ${sl:,.2f} (-{STOP_LOSS_PCT*100:.1f}%)\n"
        f"⏱️ Max Hold: {MAX_HOLD_MINUTES} dakika\n"
        f"━━━━━━━━━━━━━\n"
        f"📈 RSI: {info['rsi']:.1f}\n"
        f"📊 MACD: {'✅' if macd_bullish(info) else '🔴'}\n"
        f"📉 Bollinger: ✅\n"
        f"📦 Volume: {vol_emoji}\n"
        f"🎯 Score: {max(info['long_score'], info['short_score'])}/4\n"
        f"━━━━━━━━━━━━━\n"
        f"💰 Margin: ${MARGIN_PER_TRADE} x {LEVERAGE}x\n"
        f"🧪 SIMULATION"
    )
    log(msg)
    await send_telegram(session, msg)

def macd_bullish(info):
    return info["macd"] > info["macd_signal"]

async def close_trade(session, trade_id, current_price, reason):
    pos = active_positions[trade_id]
    pos["closed"] = True

    if pos["side"] == "LONG":
        pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"]
    else:
        pnl_pct = (pos["entry_price"] - current_price) / pos["entry_price"]

    pnl_usd = pnl_pct * pos["margin"] * LEVERAGE

    if pnl_usd > 0:
        stats["wins"] += 1
    else:
        stats["losses"] += 1
    stats["total_pnl"] += pnl_usd

    hold_min = (time.time() - pos["entry_time"]) / 60
    win_rate = (stats["wins"] / max(stats["total_trades"], 1)) * 100

    if reason == "TP":
        emoji = "💰 TAKE PROFIT!"
    elif reason == "SL":
        emoji = "🛑 STOP LOSS!"
    else:
        emoji = "⏱️ FORCE CLOSE!"

    msg = (
        f"{emoji}\n"
        f"📊 {pos['symbol']}\n"
        f"{'🟢 LONG' if pos['side'] == 'LONG' else '🔴 SHORT'}\n"
        f"💲 Entry: ${pos['entry_price']:,.2f}\n"
        f"💲 Exit: ${current_price:,.2f}\n"
        f"⏱️ Hold: {hold_min:.1f} dakika\n"
        f"━━━━━━━━━━━━━\n"
        f"💵 PnL: {'+' if pnl_usd >= 0 else ''}{pnl_usd:.2f} USDT\n"
        f"📊 Total PnL: ${stats['total_pnl']:.2f} USDT\n"
        f"🏆 Win Rate: {win_rate:.1f}%\n"
        f"✅ {stats['wins']} Wins | ❌ {stats['losses']} Losses\n"
        f"🧪 SIMULATION"
    )
    log(msg)
    await send_telegram(session, msg)

async def monitor_positions(session):
    while True:
        try:
            for trade_id, pos in list(active_positions.items()):
                if pos["closed"]:
                    continue

                current_price = await get_current_price(session, pos["symbol"])
                if current_price == 0:
                    continue

                elapsed_min = (time.time() - pos["entry_time"]) / 60

                tp_hit = (pos["side"] == "LONG" and current_price >= pos["tp_price"]) or \
                         (pos["side"] == "SHORT" and current_price <= pos["tp_price"])
                sl_hit = (pos["side"] == "LONG" and current_price <= pos["sl_price"]) or \
                         (pos["side"] == "SHORT" and current_price >= pos["sl_price"])
                time_up = elapsed_min >= MAX_HOLD_MINUTES

                if tp_hit:
                    await close_trade(session, trade_id, current_price, "TP")
                elif sl_hit:
                    await close_trade(session, trade_id, current_price, "SL")
                elif time_up:
                    await close_trade(session, trade_id, current_price, "TIME")

            await asyncio.sleep(10)

        except Exception as e:
            log(f"Monitor error: {e}")
            await asyncio.sleep(10)

async def run_scalper(session):
    while True:
        try:
            for symbol in [SYMBOL_1, SYMBOL_2]:
                open_count = sum(
                    1 for p in active_positions.values()
                    if symbol in p["symbol"] and not p["closed"]
                )
                if open_count >= 1:
                    log(f"⏳ {symbol} — trade iko wazi tayari")
                    continue

                closes, volumes = await get_candles(session, symbol)
                if not closes:
                    continue

                signal, info = analyze_market(closes, volumes)

                if signal == "WAIT":
                    stats["skipped"] += 1
                    log(
                        f"⏳ {symbol} WAIT | "
                        f"RSI:{info.get('rsi', 0):.0f} | "
                        f"L:{info.get('long_score', 0)}/4 "
                        f"S:{info.get('short_score', 0)}/4"
                    )
                else:
                    await open_trade(session, symbol, signal, info)

                await asyncio.sleep(3)

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            log(f"Scalper error: {e}")
            await asyncio.sleep(10)

async def print_stats(session):
    while True:
        await asyncio.sleep(300)
        win_rate = (stats["wins"] / max(stats["total_trades"], 1)) * 100
        open_trades = sum(1 for p in active_positions.values() if not p["closed"])
        msg = (
            f"📊 RIPOTI YA DAKIKA 5\n"
            f"⚡ Trades: {stats['total_trades']}\n"
            f"🔓 Wazi sasa: {open_trades}\n"
            f"⏳ Skipped: {stats['skipped']}\n"
            f"✅ Wins: {stats['wins']} | ❌ Losses: {stats['losses']}\n"
            f"🏆 Win Rate: {win_rate:.1f}%\n"
            f"💰 Total PnL: ${stats['total_pnl']:.2f} USDT\n"
            f"🎯 TP: {TAKE_PROFIT_PCT*100:.1f}% | SL: {STOP_LOSS_PCT*100:.1f}%\n"
            f"🧪 SIMULATION"
        )
        log(msg)
        await send_telegram(session, msg)

async def main():
    async with aiohttp.ClientSession() as session:
        start_msg = (
            f"⚡ OKX DUAL FUTURES SCALPER V3!\n"
            f"📊 BTC + ETH Futures\n"
            f"⏱️ Timeframe: {TIMEFRAME}\n"
            f"🔢 Leverage: {LEVERAGE}x\n"
            f"💰 Margin: ${MARGIN_PER_TRADE}/trade\n"
            f"🎯 TP: {TAKE_PROFIT_PCT*100:.1f}%\n"
            f"🛑 SL: {STOP_LOSS_PCT*100:.1f}%\n"
            f"⏱️ Force Close: {MAX_HOLD_MINUTES} dakika\n"
            f"📈 RSI + MACD + BB + Volume\n"
            f"🧪 SIMULATION"
        )
        log(start_msg)
        await send_telegram(session, start_msg)

        await asyncio.gather(
            run_scalper(session),
            monitor_positions(session),
            print_stats(session)
        )

if __name__ == "__main__":
    asyncio.run(main())
