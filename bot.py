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
OKX_FEE = 0.0005 * 2
MIN_NET_PROFIT = 0.015
SL_RATIO = 0.5
MIN_VOLUME_USD = 1_000_000
MIN_CHANGE_PCT = 1.0
MAX_CHANGE_PCT = 10.0
SCAN_INTERVAL = 300
MONITOR_INTERVAL = 10
MARGIN_GROWTH_X = 2.0
MARGIN_GROWTH_PCT = 0.50

active_trade = None
stats = {"wins": 0, "losses": 0, "pnl": 0.0, "fees": 0.0}
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
return {"OK-ACCESS-KEY": OKX_API_KEY, "OK-ACCESS-SIGN": sig, "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE, "Content-Type": "application/json"}

def calc_tp_sl(signal, price, high24h, low24h):
tp_pct = OKX_FEE + MIN_NET_PROFIT
sl_pct = tp_pct * SL_RATIO
if signal == "LONG":
if (high24h - price) / price < tp_pct:
return None, None
return round(price * (1 + tp_pct), 6), round(price * (1 - sl_pct), 6)
else:
if (price - low24h) / price < tp_pct:
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
opens  = [float(c[1]) for c in candles]
highs  = [float(c[2]) for c in candles]
lows   = [float(c[3]) for c in candles]
closes = [float(c[4]) for c in candles]
volumes= [float(c[5]) for c in candles]
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

def get_trend_1h(closes):
"""Trend ya 1H peke yake - rahisi na ya kuaminika"""
if len(closes) < 20:
return "SIDEWAYS"
e10 = ema(closes, 10)
e20 = ema(closes, 20)
price = closes[-1]
last_5 = closes[-5:]
going_up = last_5[-1] > last_5[0]
going_down = last_5[-1] < last_5[0]
if price > e10 > e20 and going_up:
return "UPTREND"
elif price < e10 < e20 and going_down:
return "DOWNTREND"
return "SIDEWAYS"

def is_fake_breakout(opens, highs, lows, closes, signal):
"""Gundua fake breakout kwa wick analysis"""
if len(closes) < 3:
return False
c = closes[-1]
o = opens[-1]
h = highs[-1]
l = lows[-1]
body = abs(c - o)
candle_range = h - l
if candle_range == 0:
return False
upper_wick = h - max(c, o)
lower_wick = min(c, o) - l
if signal == "LONG" and upper_wick > body * 1.5:
return True
if signal == "SHORT" and lower_wick > body * 1.5:
return True
return False

def near_resistance(highs, lows, price, signal):
"""Angalia kama bei iko karibu na resistance/support ya hivi karibuni"""
if len(highs) < 10:
return False
recent_high = max(highs[-15:])
recent_low = min(lows[-15:])
if signal == "LONG" and (recent_high - price) / price < 0.003:
return True
if signal == "SHORT" and (price - recent_low) / price < 0.003:
return True
return False

def analyze_dual(opens, highs, lows, closes, volumes, trend, high24h, low24h):
"""
Dual analysis - inagundua LONG na SHORT kwa usawa
Conditions zimepunguzwa ili ziwe za kweli na zinazoweza kufikiwa
"""
if len(closes) < 30:
return "WAIT", {}

price = closes[-1]  
r = rsi(closes)  
e9  = ema(closes, 9)  
e21 = ema(closes, 21)  

range_24h = high24h - low24h  
price_pos = (price - low24h) / range_24h * 100 if range_24h > 0 else 50  

vol_ok = False  
if len(volumes) >= 8:  
    vol_ok = sum(volumes[-3:]) / 3 > sum(volumes[-8:-3]) / 5 * 1.3  

candle_body = abs(closes[-1] - opens[-1])  
candle_range = highs[-1] - lows[-1]  
strong_candle = (candle_body / candle_range > 0.4) if candle_range > 0 else False  

momentum = (closes[-1] - closes[-4]) / closes[-4] * 100 if len(closes) >= 4 and closes[-4] > 0 else 0  

info = {  
    "rsi": r, "price_pos": price_pos, "vol_ok": vol_ok,  
    "strong_candle": strong_candle, "momentum": momentum,  
    "trend": trend, "high24h": high24h, "low24h": low24h, "price": price,  
    "e9": e9, "e21": e21  
}  

if trend == "UPTREND":  
    long_score = sum([  
        25 < r < 58,  
        e9 > e21,  
        price > e9,  
        price_pos < 50,  
        vol_ok,  
        strong_candle,  
        momentum > 0.1,  
    ])  
    info["score"] = long_score  
    if long_score >= 5:  
        if is_fake_breakout(opens, highs, lows, closes, "LONG"):  
            log(f"⚠️ Fake breakout LONG — inaruka")  
            return "WAIT", info  
        if near_resistance(highs, lows, price, "LONG"):  
            log(f"⚠️ Karibu na resistance — inaruka LONG")  
            return "WAIT", info  
        return "LONG", info  

elif trend == "DOWNTREND":  
    short_score = sum([  
        42 < r < 75,  
        e9 < e21,  
        price < e9,  
        price_pos > 50,  
        vol_ok,  
        strong_candle,  
        momentum < -0.1,  
    ])  
    info["score"] = short_score  
    if short_score >= 5:  
        if is_fake_breakout(opens, highs, lows, closes, "SHORT"):  
            log(f"⚠️ Fake breakout SHORT — inaruka")  
            return "WAIT", info  
        if near_resistance(highs, lows, price, "SHORT"):  
            log(f"⚠️ Karibu na support — inaruka SHORT")  
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

long_candidates = []  
short_candidates = []  

for coin in candidates[:40]:  
    inst_id = coin["instId"]  
    _, _, _, closes_1h, _ = await get_candles(session, inst_id, "1H", 30)  
    if not closes_1h:  
        continue  

    trend = get_trend_1h(closes_1h)  

    if trend == "SIDEWAYS":  
        await asyncio.sleep(0.1)  
        continue  

    opens_15m, highs_15m, lows_15m, closes_15m, volumes_15m = await get_candles(session, inst_id, "15m", 60)  
    if not closes_15m:  
        continue  

    signal, info = analyze_dual(  
        opens_15m, highs_15m, lows_15m, closes_15m, volumes_15m,  
        trend, coin["high24h"], coin["low24h"]  
    )  

    if signal == "WAIT":  
        await asyncio.sleep(0.1)  
        continue  

    tp, sl = calc_tp_sl(signal, coin["price"], coin["high24h"], coin["low24h"])  
    if tp is None:  
        await asyncio.sleep(0.1)  
        continue  

    score = info.get("score", 0)  
    log(f"✅ {inst_id} | {signal} | {trend} | RSI:{info['rsi']:.1f} | Pos:{info['price_pos']:.0f}% | Score:{score}/7")  

    if signal == "LONG":  
        long_candidates.append((score, inst_id, signal, coin["price"], info, tp, sl, trend))  
    else:  
        short_candidates.append((score, inst_id, signal, coin["price"], info, tp, sl, trend))  

    await asyncio.sleep(0.2)  

all_candidates = long_candidates + short_candidates  
if not all_candidates:  
    return None, "WAIT", 0, {}, 0, 0, ""  

all_candidates.sort(key=lambda x: x[0], reverse=True)  
best = all_candidates[0]  
_, inst_id, signal, price, info, tp, sl, trend = best  

log(f"🏆 Bora: {inst_id} | {signal} | Score:{best[0]}/7")  
return inst_id, signal, price, info, tp, sl, trend

async def open_trade(session, inst_id, signal, price, info, tp, sl, trend):
global active_trade

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

active_trade = {  
    "inst_id": inst_id, "signal": signal, "pos_side": pos_side,  
    "entry_price": price, "total_margin": margin_1,  
    "tp_price": tp, "sl_price": sl,  
    "entry_time": time.time(), "added": False,  
    "closed": False, "fee_est": fee_est  
}  

coin_name = inst_id.replace("-SWAP", "")  
tp_pct = abs(tp - price) / price * 100  
sl_pct = abs(sl - price) / price * 100  
net_tp = (tp_pct / 100 - OKX_FEE) * position_size  
net_sl = (sl_pct / 100 + OKX_FEE) * position_size  
score = info.get("score", 0)  
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
    f"✅ Net faida: +${net_tp:.4f}\n"  
    f"❌ Net hasara: -${net_sl:.4f}\n"  
    f"📊 Ratio: {net_tp/net_sl:.1f}:1\n"  
    f"━━━━━━━━━━━━━━━\n"  
    f"🧠 Trend 1H: {trend}\n"  
    f"📊 RSI: {info.get('rsi', 0):.1f}\n"  
    f"📍 Position 24h: {info.get('price_pos', 0):.0f}%\n"  
    f"📦 Volume: {'✅' if info.get('vol_ok') else '❌'}\n"  
    f"🕯️ Candle: {'✅' if info.get('strong_candle') else '❌'}\n"  
    f"⚡ Momentum: {info.get('momentum', 0):+.2f}%\n"  
    f"🎯 Score: {score}/7\n"  
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
            trade["fee_est"] += position_2 * OKX_FEE  
            trade["added"] = True  
            mode = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"  
            await send_telegram(session, f"➕ IMEONGEZA!\n📊 {coin_name}\n💲 {price}\n💰 +${margin_2:.2f}\n📈 {change*100:+.2f}%\n⚡ {mode}")  

        tp_hit = (trade["signal"] == "LONG" and price >= trade["tp_price"]) or (trade["signal"] == "SHORT" and price <= trade["tp_price"])  
        sl_hit = (trade["signal"] == "LONG" and price <= trade["sl_price"]) or (trade["signal"] == "SHORT" and price >= trade["sl_price"])  

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
await send_telegram(session, f"📈 MARGIN IMEONGEZWA!\n💰 Balance: ${balance:.4f}\n📊 ${old:.2f} → ${current_margin:.2f}\n🎯 Itaongezwa: ${balance * MARGIN_GROWTH_X:.2f}")

async def scanner_loop(session):
global active_trade
await asyncio.sleep(5)
while True:
try:
if active_trade and not active_trade["closed"]:
log(f"⏳ Trade wazi: {active_trade['inst_id']}")
await asyncio.sleep(SCAN_INTERVAL)
continue
log("🔍 Inascan LONG na SHORT...")
result = await scan_best_coin(session)
if result[0] is None:
log("Hakuna signal — inasubiri...")
await asyncio.sleep(SCAN_INTERVAL)
continue
inst_id, signal, price, info, tp, sl, trend = result
await open_trade(session, inst_id, signal, price, info, tp, sl, trend)
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
tp_pct = (OKX_FEE + MIN_NET_PROFIT) * 100
sl_pct = tp_pct * SL_RATIO
mode = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"
msg = (
f"⚡ OKX DUAL FUTURES — SMART!\n"
f"━━━━━━━━━━━━━━━\n"
f"💰 Balance: ${balance:.4f} USDT\n"
f"🔢 Leverage: {LEVERAGE}x | Isolated\n"
f"🟢 LONG + 🔴 SHORT zote mbili\n"
f"━━━━━━━━━━━━━━━\n"
f"🧠 Akili za entry:\n"
f"✅ Trend 1H (UPTREND/DOWNTREND)\n"
f"✅ RSI iliyopimwa vizuri\n"
f"✅ EMA alignment\n"
f"✅ Position 24h balanced\n"
f"✅ Volume 1.3x surge\n"
f"✅ Fake breakout check\n"
f"✅ Support/Resistance check\n"
f"✅ Score 5/7 minimum\n"
f"━━━━━━━━━━━━━━━\n"
f"🎯 TP: +{tp_pct:.2f}% | SL: -{sl_pct:.2f}%\n"
f"⚡ Mode: {mode}"
)
log(msg)
await send_telegram(session, msg)
await asyncio.gather(scanner_loop(session), monitor_trade(session))

if name == "main":
asyncio.run(main())
