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

TELEGRAM_TOKEN = "8778061073:AAFvbdcKusf3P74VLTzdcYa7obV2LrgDXyE"
TELEGRAM_CHAT_ID = "7010983039"

OKX_BASE = "https://www.okx.com"

DRY_RUN = True

SYMBOL_1 = "BTC-USDT-SWAP"
SYMBOL_2 = "ETH-USDT-SWAP"

LEVERAGE = 10
MARGIN_PER_TRADE = 5
TAKE_PROFIT_PCT = 0.008
STOP_LOSS_PCT = 0.004
MAX_TRADES_PER_SYMBOL = 2
RSI_PERIOD = 14
CHECK_INTERVAL = 30

stats = {
    "total_trades": 0,
    "wins": 0,
    "losses": 0,
    "total_pnl": 0.0
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

async def get_candles(session, symbol, bar="1m", limit=50):
    try:
        path = f"/api/v5/market/candles?instId={symbol}&bar={bar}&limit={limit}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            candles = data.get("data", [])
            closes = [float(c[4]) for c in reversed(candles)]
            return closes
    except Exception as e:
        log(f"Candles error: {e}")
        return []

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

    gains = []
    losses = []

    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_ema(closes, period):
    if len(closes) < period:
        return closes[-1] if closes else 0

    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period

    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema

    return ema

def get_signal(closes):
    if len(closes) < 20:
        return "WAIT"

    rsi = calculate_rsi(closes, RSI_PERIOD)
    ema_fast = calculate_ema(closes, 9)
    ema_slow = calculate_ema(closes, 21)
    current_price = closes[-1]

    if rsi < 35 and ema_fast > ema_slow and current_price > ema_fast:
        return "LONG"

    elif rsi > 65 and ema_fast < ema_slow and current_price < ema_fast:
        return "SHORT"

    return "WAIT"

async def place_order(session, symbol, side, size):
    if DRY_RUN:
        price = await get_current_price(session, symbol)
        log(f"[SIMULATION] {side} {symbol} @ ${price:.2f} | Size: {size}")
        return {"ordId": f"sim_{int(time.time())}", "price": price}

    try:
        path = "/api/v5/trade/order"
        body = json.dumps({
            "instId": symbol,
            "tdMode": "cross",
            "side": side.lower(),
            "ordType": "market",
            "sz": str(size),
            "posSide": "long" if side == "BUY" else "short"
        })
        headers = get_okx_headers("POST", path, body)
        async with session.post(OKX_BASE + path, headers=headers, data=body) as r:
            data = await r.json()
            return data.get("data", [{}])[0]
    except Exception as e:
        log(f"Order error: {e}")
        return None

async def set_leverage(session, symbol):
    if DRY_RUN:
        return

    try:
        path = "/api/v5/account/set-leverage"
        body = json.dumps({
            "instId": symbol,
            "lever": str(LEVERAGE),
            "mgnMode": "cross"
        })
        headers = get_okx_headers("POST", path, body)
        async with session.post(OKX_BASE + path, headers=headers, data=body) as r:
            await r.json()
    except Exception as e:
        log(f"Leverage error: {e}")

async def scalp_symbol(session, symbol):
    symbol_positions = {k: v for k, v in active_positions.items() if k.startswith(symbol)}
    if len(symbol_positions) >= MAX_TRADES_PER_SYMBOL:
        return

    closes = await get_candles(session, symbol)
    if not closes:
        return

    signal = get_signal(closes)
    if signal == "WAIT":
        return

    current_price = closes[-1]
    rsi = calculate_rsi(closes, RSI_PERIOD)
    ema_fast = calculate_ema(closes, 9)
    ema_slow = calculate_ema(closes, 21)

    if signal == "LONG":
        tp_price = current_price * (1 + TAKE_PROFIT_PCT)
        sl_price = current_price * (1 - STOP_LOSS_PCT)
        side = "BUY"
    else:
        tp_price = current_price * (1 - TAKE_PROFIT_PCT)
        sl_price = current_price * (1 + STOP_LOSS_PCT)
        side = "SELL"

    size = round((MARGIN_PER_TRADE * LEVERAGE) / current_price, 4)

    await set_leverage(session, symbol)
    result = await place_order(session, symbol, side, size)

    if not result:
        return

    trade_id = f"{symbol}_{int(time.time())}"
    active_positions[trade_id] = {
        "symbol": symbol,
        "side": signal,
        "entry_price": current_price,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "size": size,
        "entry_time": time.time(),
        "margin": MARGIN_PER_TRADE,
        "closed": False
    }

    stats["total_trades"] += 1

    msg = (
        f"⚡ SCALP TRADE IMEFUNGULIWA!\n"
        f"📊 Symbol: {symbol}\n"
        f"{'🟢 LONG' if signal == 'LONG' else '🔴 SHORT'}\n"
        f"💲 Bei: ${current_price:.2f}\n"
        f"🎯 Take Profit: ${tp_price:.2f}\n"
        f"🛑 Stop Loss: ${sl_price:.2f}\n"
        f"📈 RSI: {rsi:.1f}\n"
        f"📊 EMA9: ${ema_fast:.2f} | EMA21: ${ema_slow:.2f}\n"
        f"💰 Margin: ${MARGIN_PER_TRADE} | Leverage: {LEVERAGE}x\n"
        f"🔢 Trade #{stats['total_trades']}\n"
        f"🧪 SIMULATION"
    )
    log(msg)
    await send_telegram(session, msg)

async def monitor_positions(session):
    while True:
        try:
            closed_ids = []

            for trade_id, pos in active_positions.items():
                if pos["closed"]:
                    closed_ids.append(trade_id)
                    continue

                current_price = await get_current_price(session, pos["symbol"])
                if current_price == 0:
                    continue

                if pos["side"] == "LONG":
                    pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"]
                else:
                    pnl_pct = (pos["entry_price"] - current_price) / pos["entry_price"]

                pnl_usd = pnl_pct * pos["margin"] * LEVERAGE

                tp_hit = (pos["side"] == "LONG" and current_price >= pos["tp_price"]) or \
                         (pos["side"] == "SHORT" and current_price <= pos["tp_price"])

                sl_hit = (pos["side"] == "LONG" and current_price <= pos["sl_price"]) or \
                         (pos["side"] == "SHORT" and current_price >= pos["sl_price"])

                if tp_hit or sl_hit:
                    pos["closed"] = True
                    closed_ids.append(trade_id)

                    if tp_hit:
                        stats["wins"] += 1
                        stats["total_pnl"] += pnl_usd
                        result_emoji = "💰 TAKE PROFIT!"
                    else:
                        stats["losses"] += 1
                        stats["total_pnl"] += pnl_usd
                        result_emoji = "🛑 STOP LOSS!"

                    win_rate = (stats["wins"] / max(stats["total_trades"], 1)) * 100

                    msg = (
                        f"{result_emoji}\n"
                        f"📊 {pos['symbol']}\n"
                        f"{'🟢 LONG' if pos['side'] == 'LONG' else '🔴 SHORT'}\n"
                        f"💲 Entry: ${pos['entry_price']:.2f}\n"
                        f"💲 Exit: ${current_price:.2f}\n"
                        f"📈 PnL: {'+' if pnl_usd > 0 else ''}{pnl_usd:.2f} USDT\n"
                        f"📊 Total PnL: ${stats['total_pnl']:.2f}\n"
                        f"🏆 Win Rate: {win_rate:.1f}%\n"
                        f"✅ Wins: {stats['wins']} | ❌ Losses: {stats['losses']}\n"
                        f"🧪 SIMULATION"
                    )
                    log(msg)
                    await send_telegram(session, msg)

            for trade_id in closed_ids:
                if trade_id in active_positions:
                    del active_positions[trade_id]

            await asyncio.sleep(5)

        except Exception as e:
            log(f"Monitor error: {e}")
            await asyncio.sleep(5)

async def run_dual_scalper(session):
    log("Dual Futures Scalper inaanza...")

    while True:
        try:
            await scalp_symbol(session, SYMBOL_1)
            await asyncio.sleep(2)
            await scalp_symbol(session, SYMBOL_2)
            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            log(f"Scalper error: {e}")
            await asyncio.sleep(10)

async def print_stats(session):
    while True:
        await asyncio.sleep(300)
        win_rate = (stats["wins"] / max(stats["total_trades"], 1)) * 100
        msg = (
            f"📊 RIPOTI YA DAKIKA 5\n"
            f"⚡ Trades: {stats['total_trades']}\n"
            f"✅ Wins: {stats['wins']} | ❌ Losses: {stats['losses']}\n"
            f"🏆 Win Rate: {win_rate:.1f}%\n"
            f"💰 Total PnL: ${stats['total_pnl']:.2f} USDT\n"
            f"📊 BTC: {SYMBOL_1}\n"
            f"📊 ETH: {SYMBOL_2}\n"
            f"🔢 Leverage: {LEVERAGE}x\n"
            f"🧪 SIMULATION"
        )
        log(msg)
        await send_telegram(session, msg)

async def main():
    async with aiohttp.ClientSession() as session:
        start_msg = (
            f"⚡ OKX DUAL FUTURES SCALPING BOT!\n"
            f"📊 Symbol 1: {SYMBOL_1}\n"
            f"📊 Symbol 2: {SYMBOL_2}\n"
            f"🔢 Leverage: {LEVERAGE}x\n"
            f"💰 Margin/Trade: ${MARGIN_PER_TRADE}\n"
            f"🎯 Take Profit: {TAKE_PROFIT_PCT*100:.1f}%\n"
            f"🛑 Stop Loss: {STOP_LOSS_PCT*100:.1f}%\n"
            f"📈 Indicators: RSI + EMA Cross\n"
            f"🧪 Mode: {'SIMULATION' if DRY_RUN else 'LIVE'}"
        )
        log(start_msg)
        await send_telegram(session, start_msg)

        await asyncio.gather(
            run_dual_scalper(session),
            monitor_positions(session),
            print_stats(session)
        )

if __name__ == "__main__":
    asyncio.run(main())
