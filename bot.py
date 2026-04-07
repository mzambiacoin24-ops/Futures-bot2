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
TAKE_PROFIT_PCT = 0.008
STOP_LOSS_PCT = 0.004
MAX_TRADES_PER_SYMBOL = 2
RSI_PERIOD = 14
CHECK_INTERVAL = 30

# 🔥 NEW CONTROL
last_report = None
last_report_time = 0
MESSAGE_COOLDOWN = 60

stats = {"total_trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
active_positions = {}

def log(msg):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}")

async def send_telegram(session, msg):
    global last_report_time
    try:
        now = time.time()
        if now - last_report_time < MESSAGE_COOLDOWN:
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        await session.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
        last_report_time = now
    except Exception as e:
        log(f"Telegram error: {e}")

def get_okx_headers(method, path, body=""):
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    message = timestamp + method.upper() + path + body
    signature = base64.b64encode(
        hmac.new(OKX_SECRET.encode(), message.encode(), hashlib.sha256).digest()
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
            closes = [float(c[4]) for c in reversed(data.get("data", []))]
            return closes
    except:
        return []

async def get_current_price(session, symbol):
    try:
        path = f"/api/v5/market/ticker?instId={symbol}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            return float(data.get("data", [{}])[0].get("last", 0))
    except:
        return 0.0

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

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
    price = closes[-1]
    if rsi < 35 and ema_fast > ema_slow and price > ema_fast:
        return "LONG"
    elif rsi > 65 and ema_fast < ema_slow and price < ema_fast:
        return "SHORT"
    return "WAIT"

async def scalp_symbol(session, symbol):
    symbol_positions = [k for k in active_positions if symbol in k]
    if len(symbol_positions) >= MAX_TRADES_PER_SYMBOL:
        return

    closes = await get_candles(session, symbol)
    if not closes:
        return

    signal = get_signal(closes)
    if signal == "WAIT":
        return

    price = closes[-1]

    tp = price * (1 + TAKE_PROFIT_PCT) if signal == "LONG" else price * (1 - TAKE_PROFIT_PCT)
    sl = price * (1 - STOP_LOSS_PCT) if signal == "LONG" else price * (1 + STOP_LOSS_PCT)

    trade_id = f"{symbol}_{int(time.time())}"

    active_positions[trade_id] = {
        "symbol": symbol,
        "side": signal,
        "entry_price": price,
        "tp_price": tp,
        "sl_price": sl,
        "margin": MARGIN_PER_TRADE,
        "closed": False
    }

    stats["total_trades"] += 1

    msg = f"⚡ TRADE {signal}\n📊 {symbol}\n💲 {price:.2f}\nTP {tp:.2f} | SL {sl:.2f}"
    log(msg)
    await send_telegram(session, msg)

async def monitor_positions(session):
    while True:
        for trade_id, pos in list(active_positions.items()):
            price = await get_current_price(session, pos["symbol"])
            if not price:
                continue

            tp_hit = price >= pos["tp_price"] if pos["side"] == "LONG" else price <= pos["tp_price"]
            sl_hit = price <= pos["sl_price"] if pos["side"] == "LONG" else price >= pos["sl_price"]

            if tp_hit or sl_hit:
                pnl = (price - pos["entry_price"]) if pos["side"] == "LONG" else (pos["entry_price"] - price)
                pnl *= LEVERAGE

                if tp_hit:
                    stats["wins"] += 1
                else:
                    stats["losses"] += 1

                stats["total_pnl"] += pnl

                msg = f"📉 CLOSE {pos['symbol']} @ {price:.2f}\nPnL {pnl:.2f}"
                await send_telegram(session, msg)

                del active_positions[trade_id]

        await asyncio.sleep(5)

async def print_stats(session):
    global last_report
    while True:
        await asyncio.sleep(300)

        win_rate = (stats["wins"] / max(stats["total_trades"], 1)) * 100

        msg = f"📊 Trades {stats['total_trades']} | Wins {stats['wins']} | Loss {stats['losses']} | PnL {stats['total_pnl']:.2f}"

        if msg == last_report:
            continue

        last_report = msg
        await send_telegram(session, msg)

async def main():
    async with aiohttp.ClientSession() as session:
        await send_telegram(session, "🚀 BOT STARTED")

        await asyncio.gather(
            run_dual_scalper(session),
            monitor_positions(session),
            print_stats(session)
        )

async def run_dual_scalper(session):
    while True:
        await scalp_symbol(session, SYMBOL_1)
        await asyncio.sleep(2)
        await scalp_symbol(session, SYMBOL_2)
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
