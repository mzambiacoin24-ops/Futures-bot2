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
TAKE_PROFIT_PCT = 0.012
STOP_LOSS_PCT = 0.006
MAX_TRADES_PER_SYMBOL = 1
CHECK_INTERVAL = 60

RSI_PERIOD = 14
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
BB_PERIOD = 20
BB_STD = 2.0
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

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
            candles = data.get("data", [])
            candles = list(reversed(candles))
            closes = [float(c[4]) for c in candles]
            highs = [float(c[2]) for c in candles]
            lows = [float(c[3]) for c in candles]
            volumes = [float(c[5]) for c in candles]
            return closes, highs, lows, volumes
    except Exception as e:
        log(f"Candles error: {e}")
        return [], [], [], []

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

def calculate_macd(closes):
    if len(closes) < MACD_SLOW + MACD_SIGNAL:
        return 0, 0, 0
    ema_fast = calculate_ema(closes, MACD_FAST)
    ema_slow = calculate_ema(closes, MACD_SLOW)
    macd_line = ema_fast - ema_slow

    macd_values = []
    for i in range(MACD_SIGNAL, len(closes)):
        ef = calculate_ema(closes[:i], MACD_FAST)
        es = calculate_ema(closes[:i], MACD_SLOW)
        macd_values.append(ef - es)

    if len(macd_values) < MACD_SIGNAL:
        return macd_line, 0, macd_line

    signal_line = calculate_ema(macd_values, MACD_SIGNAL)
    histogram = macd_line - signal_line

    prev_macd = macd_values[-2] if len(macd_values) >= 2 else macd_line
    prev_signal = calculate_ema(macd_values[:-1], MACD_SIGNAL) if len(macd_values) > MACD_SIGNAL else signal_line

    return macd_line, signal_line, histogram

def calculate_bollinger_bands(closes, period=20, std_dev=2.0):
    if len(closes) < period:
        price = closes[-1] if closes else 0
        return price, price, price
    recent = closes[-period:]
    middle = sum(recent) / period
    variance = sum((x - middle) ** 2 for x in recent) / period
    std = variance ** 0.5
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower

def calculate_volume_trend(volumes):
    if len(volumes) < 10:
        return False
    recent_vol = sum(volumes[-5:]) / 5
    prev_vol = sum(volumes[-10:-5]) / 5
    return recent_vol > prev_vol * 1.2

def get_signal(closes, highs, lows, volumes):
    if len(closes) < 50:
        return "WAIT", {}

    rsi = calculate_rsi(closes, RSI_PERIOD)
    macd_line, signal_line, histogram = calculate_macd(closes)
    bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(closes, BB_PERIOD, BB_STD)
    vol_increasing = calculate_volume_trend(volumes)
    current_price = closes[-1]

    prev_macd_values = []
    for i in range(MACD_SIGNAL + 1, len(closes)):
        ef = calculate_ema(closes[:i], MACD_FAST)
        es = calculate_ema(closes[:i], MACD_SLOW)
        prev_macd_values.append(ef - es)

    macd_crossed_up = False
    macd_crossed_down = False

    if len(prev_macd_values) >= MACD_SIGNAL + 1:
        prev_macd = prev_macd_values[-2]
        prev_signal = calculate_ema(prev_macd_values[:-1], MACD_SIGNAL)
        curr_signal = calculate_ema(prev_macd_values, MACD_SIGNAL)
        macd_crossed_up = prev_macd < prev_signal and macd_line > signal_line
        macd_crossed_down = prev_macd > prev_signal and macd_line < signal_line

    indicators = {
        "rsi": rsi,
        "macd": macd_line,
        "signal": signal_line,
        "histogram": histogram,
        "bb_upper": bb_upper,
        "bb_middle": bb_middle,
        "bb_lower": bb_lower,
        "price": current_price,
        "vol_increasing": vol_increasing,
        "macd_crossed_up": macd_crossed_up,
        "macd_crossed_down": macd_crossed_down
    }

    long_conditions = [
        rsi < RSI_OVERSOLD,
        macd_line > signal_line or macd_crossed_up,
        current_price <= bb_lower * 1.005,
        vol_increasing
    ]

    short_conditions = [
        rsi > RSI_OVERBOUGHT,
        macd_line < signal_line or macd_crossed_down,
        current_price >= bb_upper * 0.995,
        vol_increasing
    ]

    long_score = sum(long_conditions)
    short_score = sum(short_conditions)

    indicators["long_score"] = long_score
    indicators["short_score"] = short_score

    if long_score >= 3:
        return "LONG", indicators
    elif short_score >= 3:
        return "SHORT", indicators

    return "WAIT", indicators

async def place_order(session, symbol, side, size):
    if DRY_RUN:
        price = await get_current_price(session, symbol)
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

async def scalp_symbol(session, symbol):
    symbol_positions = {k: v for k, v in active_positions.items()
                        if symbol in k and not v["closed"]}
    if len(symbol_positions) >= MAX_TRADES_PER_SYMBOL:
        return

    closes, highs, lows, volumes = await get_candles(session, symbol, TIMEFRAME)
    if not closes:
        return

    signal, indicators = get_signal(closes, highs, lows, volumes)

    if signal == "WAIT":
        stats["skipped"] += 1
        log(
            f"⏳ {symbol} — WAIT | "
            f"RSI: {indicators.get('rsi', 0):.1f} | "
            f"Long: {indicators.get('long_score', 0)}/4 | "
            f"Short: {indicators.get('short_score', 0)}/4"
        )
        return

    current_price = closes[-1]

    if signal == "LONG":
        tp_price = current_price * (1 + TAKE_PROFIT_PCT)
        sl_price = current_price * (1 - STOP_LOSS_PCT)
        side = "BUY"
    else:
        tp_price = current_price * (1 - TAKE_PROFIT_PCT)
        sl_price = current_price * (1 + STOP_LOSS_PCT)
        side = "SELL"

    size = round((MARGIN_PER_TRADE * LEVERAGE) / current_price, 4)
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

    rsi = indicators.get("rsi", 0)
    long_score = indicators.get("long_score", 0)
    short_score = indicators.get("short_score", 0)
    vol_ok = "✅" if indicators.get("vol_increasing") else "❌"

    msg = (
        f"⚡ SCALP TRADE!\n"
        f"📊 {symbol}\n"
        f"{'🟢 LONG' if signal == 'LONG' else '🔴 SHORT'}\n"
        f"💲 Bei: ${current_price:.2f}\n"
        f"🎯 TP: ${tp_price:.2f} (+{TAKE_PROFIT_PCT*100:.1f}%)\n"
        f"🛑 SL: ${sl_price:.2f} (-{STOP_LOSS_PCT*100:.1f}%)\n"
        f"━━━━━━━━━━━━━\n"
        f"📈 RSI: {rsi:.1f}\n"
        f"📊 MACD: {'✅' if indicators.get('macd_crossed_up') or indicators.get('macd_crossed_down') else '↗️'}\n"
        f"📉 Bollinger: ✅\n"
        f"📦 Volume: {vol_ok}\n"
        f"🎯 Score: {max(long_score, short_score)}/4\n"
        f"━━━━━━━━━━━━━\n"
        f"💰 Margin: ${MARGIN_PER_TRADE} x {LEVERAGE}x\n"
        f"🔢 Trade #{stats['total_trades']}\n"
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
                    if tp_hit:
                        stats["wins"] += 1
                        stats["total_pnl"] += pnl_usd
                        result_emoji = "💰 TAKE PROFIT!"
                    else:
                        stats["losses"] += 1
                        stats["total_pnl"] += pnl_usd
                        result_emoji = "🛑 STOP LOSS!"

                    hold_min = (time.time() - pos["entry_time"]) / 60
                    win_rate = (stats["wins"] / max(stats["total_trades"], 1)) * 100

                    msg = (
                        f"{result_emoji}\n"
                        f"📊 {pos['symbol']}\n"
                        f"{'🟢 LONG' if pos['side'] == 'LONG' else '🔴 SHORT'}\n"
                        f"💲 Entry: ${pos['entry_price']:.2f}\n"
                        f"💲 Exit: ${current_price:.2f}\n"
                        f"⏱️ Hold: {hold_min:.1f} dakika\n"
                        f"━━━━━━━━━━━━━\n"
                        f"💵 PnL: {'+' if pnl_usd > 0 else ''}{pnl_usd:.2f} USDT\n"
                        f"📊 Total PnL: ${stats['total_pnl']:.2f} USDT\n"
                        f"🏆 Win Rate: {win_rate:.1f}%\n"
                        f"✅ Wins: {stats['wins']} | ❌ Losses: {stats['losses']}\n"
                        f"🧪 SIMULATION"
                    )
                    log(msg)
                    await send_telegram(session, msg)

            await asyncio.sleep(5)

        except Exception as e:
            log(f"Monitor error: {e}")
            await asyncio.sleep(5)

async def run_dual_scalper(session):
    while True:
        try:
            await scalp_symbol(session, SYMBOL_1)
            await asyncio.sleep(3)
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
            f"⏳ Skipped: {stats['skipped']}\n"
            f"✅ Wins: {stats['wins']} | ❌ Losses: {stats['losses']}\n"
            f"🏆 Win Rate: {win_rate:.1f}%\n"
            f"💰 Total PnL: ${stats['total_pnl']:.2f} USDT\n"
            f"🔢 Leverage: {LEVERAGE}x | TF: {TIMEFRAME}\n"
            f"🧪 SIMULATION"
        )
        log(msg)
        await send_telegram(session, msg)

async def main():
    async with aiohttp.ClientSession() as session:
        start_msg = (
            f"⚡ OKX DUAL FUTURES SCALPER V2!\n"
            f"📊 BTC + ETH Futures\n"
            f"⏱️ Timeframe: {TIMEFRAME}\n"
            f"🔢 Leverage: {LEVERAGE}x\n"
            f"💰 Margin: ${MARGIN_PER_TRADE}/trade\n"
            f"🎯 TP: {TAKE_PROFIT_PCT*100:.1f}% | SL: {STOP_LOSS_PCT*100:.1f}%\n"
            f"📈 Indicators: RSI + MACD + BB + Volume\n"
            f"🎯 Min Score: 3/4 kuingia trade\n"
            f"🧪 Mode: SIMULATION"
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
