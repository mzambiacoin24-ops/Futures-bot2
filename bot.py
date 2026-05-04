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

COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "DOGE-USDT-SWAP", "XRP-USDT-SWAP"]
DRY_RUN = False
LEVERAGE = 5
FIRST_ENTRY_PCT = 0.30
ADD_ENTRY_PCT = 0.20
ADD_THRESHOLD_PCT = 0.008
OKX_FEE = 0.001
TP_LEVELS = [0.008, 0.012, 0.018, 0.025]
SL_PCT = 0.01
MIN_WAIT = 30 * 60
MAX_TRADES_DAY = 20
SCAN_INTERVAL = 180
MONITOR_INTERVAL = 10
MARGIN_GROWTH_X = 2.0
MARGIN_GROWTH_PCT = 0.50
MIN_SCORE = 6

active_trade = None
stats = {"wins": 0, "losses": 0, "pnl": 0.0, "fees": 0.0}
current_margin = 0.0
last_double_at = 0.0
last_trade_time = 0
trades_today = 0
last_trade_day = ""

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

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

async def get_instrument_info(session, inst_id):
    try:
        path = f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            inst = data.get("data", [{}])[0]
            return float(inst.get("minSz", 1)), float(inst.get("ctVal", 1))
    except:
        return 1, 1

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
        sz_str = str(int(size)) if size >= 1 else str(round(float(size), 4))
        body = json.dumps({
            "instId": inst_id,
            "tdMode": "isolated",
            "side": side,
            "posSide": pos_side,
            "ordType": "market",
            "sz": sz_str,
            "clOrdId": f"bot{int(time.time())}"
        })
        log(f"Placing order: {inst_id} {side} {pos_side} sz={sz_str}")
        headers = get_headers("POST", path, body)
        async with session.post(OKX_BASE + path, headers=headers, data=body) as r:
            data = await r.json()
            log(f"Order response: code={data.get('code')} msg={data.get('msg')}")
            if data.get("code") == "0":
                result = data.get("data", [{}])[0]
                if result.get("sCode") == "0":
                    log(f"Order OK: {result.get('ordId')}")
                    return result
                log(f"Order sCode failed: {result.get('sCode')} | {result.get('sMsg')}")
            else:
                log(f"Order error: {data.get('msg')} | full: {data}")
            return None
    except Exception as e:
        log(f"Order exception: {e}")
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

async def get_candles(session, inst_id, bar="5m", limit=200):
    try:
        path = f"/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            candles = list(reversed(data.get("data", [])))
            return [float(c[1]) for c in candles], [float(c[2]) for c in candles], [float(c[3]) for c in candles], [float(c[4]) for c in candles], [float(c[5]) for c in candles]
    except:
        return [], [], [], [], []

async def get_price(session, inst_id):
    try:
        path = f"/api/v5/market/ticker?instId={inst_id}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            t = data.get("data", [{}])[0]
            return float(t.get("last", 0)), float(t.get("high24h", 0)), float(t.get("low24h", 0))
    except:
        return 0.0, 0.0, 0.0

async def get_orderbook(session, inst_id):
    try:
        path = f"/api/v5/market/books?instId={inst_id}&sz=20"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            book = data.get("data", [{}])[0]
            bids = [[float(b[0]), float(b[1])] for b in book.get("bids", [])]
            asks = [[float(a[0]), float(a[1])] for a in book.get("asks", [])]
            return bids, asks
    except:
        return [], []

async def get_trades(session, inst_id):
    try:
        path = f"/api/v5/market/trades?instId={inst_id}&limit=50"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            return data.get("data", [])
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

def detect_smart_money(bids, asks, trades):
    if not bids or not asks:
        return "NEUTRAL", 0.5
    bid_vol = sum(b[1] for b in bids[:10])
    ask_vol = sum(a[1] for a in asks[:10])
    total = bid_vol + ask_vol
    bid_ratio = bid_vol / total if total > 0 else 0.5
    buy_vol = sum(float(t.get("sz", 0)) for t in trades[:30] if t.get("side") == "buy")
    sell_vol = sum(float(t.get("sz", 0)) for t in trades[:30] if t.get("side") == "sell")
    trade_total = buy_vol + sell_vol
    trade_ratio = buy_vol / trade_total if trade_total > 0 else 0.5
    combined = (bid_ratio * 0.5) + (trade_ratio * 0.5)
    if combined > 0.55:
        return "BULLISH", combined
    elif combined < 0.45:
        return "BEARISH", combined
    return "NEUTRAL", combined

def detect_structure(highs, lows):
    if len(highs) < 20:
        return "UNKNOWN"
    hh = highs[-1] > highs[-10]
    hl = lows[-1] > lows[-10]
    lh = highs[-1] < highs[-10]
    ll = lows[-1] < lows[-10]
    if hh and hl:
        return "UPTREND"
    elif lh and ll:
        return "DOWNTREND"
    return "RANGING"

def detect_ob(opens, highs, lows, closes):
    ob_bull, ob_bear = None, None
    for i in range(max(0, len(closes)-5), len(closes)-1):
        body = abs(closes[i] - opens[i])
        cr = highs[i] - lows[i]
        if cr > 0 and body / cr > 0.6:
            if closes[i] > opens[i]:
                ob_bull = lows[i]
            else:
                ob_bear = highs[i]
    return ob_bull, ob_bear

def analyze(opens_5m, highs_5m, lows_5m, closes_5m, volumes_5m,
            highs_1h, lows_1h, closes_1h, sm_dir, sm_str, high24h, low24h):
    if len(closes_5m) < 50 or len(closes_1h) < 50:
        return "WAIT", {}
    price = closes_5m[-1]
    e21 = ema(closes_5m, 21)
    e50_5m = ema(closes_5m, 50)
    e50_1h = ema(closes_1h, 50)
    e200_1h = ema(closes_1h, 200)
    r = rsi(closes_5m, 14)
    structure = detect_structure(highs_1h, lows_1h)
    ob_bull, ob_bear = detect_ob(opens_5m, highs_5m, lows_5m, closes_5m)
    vol_ok = sum(volumes_5m[-3:]) / 3 > sum(volumes_5m[-10:-3]) / 7 * 1.4 if len(volumes_5m) >= 10 else False
    body = abs(closes_5m[-1] - opens_5m[-1])
    cr = highs_5m[-1] - lows_5m[-1]
    strong_candle = body / cr > 0.5 if cr > 0 else False
    momentum = (closes_5m[-1] - closes_5m[-4]) / closes_5m[-4] * 100 if closes_5m[-4] > 0 else 0
    range_24h = high24h - low24h
    price_pos = (price - low24h) / range_24h * 100 if range_24h > 0 else 50
    info = {"rsi": r, "e21": e21, "e50_5m": e50_5m, "e50_1h": e50_1h, "e200_1h": e200_1h,
            "structure": structure, "sm_dir": sm_dir, "sm_str": sm_str,
            "ob_bull": ob_bull, "ob_bear": ob_bear, "vol_ok": vol_ok,
            "strong_candle": strong_candle, "momentum": momentum, "price_pos": price_pos,
            "high24h": high24h, "low24h": low24h, "price": price}
    if sm_dir == "BULLISH" and structure in ["UPTREND", "RANGING"]:
        score = sum([sm_dir == "BULLISH", sm_str > 0.55, structure == "UPTREND",
                     e50_1h > e200_1h, price > e50_1h, e21 > e50_5m,
                     45 < r < 75, vol_ok, strong_candle, momentum > 0.03,
                     price_pos < 70, ob_bull is not None])
        info["score"] = score
        if score >= MIN_SCORE:
            return "LONG", info
    if sm_dir == "BEARISH" and structure in ["DOWNTREND", "RANGING"]:
        score = sum([sm_dir == "BEARISH", sm_str < 0.45, structure == "DOWNTREND",
                     e50_1h < e200_1h, price < e50_1h, e21 < e50_5m,
                     25 < r < 55, vol_ok, strong_candle, momentum < -0.03,
                     price_pos > 30, ob_bear is not None])
        info["score"] = score
        if score >= MIN_SCORE:
            return "SHORT", info
    return "WAIT", info

async def scan_coins(session):
    best_signal, best_info, best_score = "WAIT", {}, 0
    for inst_id in COINS:
        log(f"Inachunguza {inst_id}...")
        opens_5m, highs_5m, lows_5m, closes_5m, volumes_5m = await get_candles(session, inst_id, "5m", 100)
        if len(closes_5m) < 50:
            continue
        _, highs_1h, lows_1h, closes_1h, _ = await get_candles(session, inst_id, "1H", 220)
        if len(closes_1h) < 50:
            continue
        bids, asks = await get_orderbook(session, inst_id)
        trades = await get_trades(session, inst_id)
        sm_dir, sm_str = detect_smart_money(bids, asks, trades)
        price, high24h, low24h = await get_price(session, inst_id)
        if not price:
            continue
        signal, info = analyze(opens_5m, highs_5m, lows_5m, closes_5m, volumes_5m,
                                highs_1h, lows_1h, closes_1h, sm_dir, sm_str, high24h, low24h)
        if signal == "WAIT":
            log(f"  {inst_id} — Hakuna signal | SM:{sm_dir} | Structure:{info.get('structure','?')}")
            await asyncio.sleep(1)
            continue
        score = info.get("score", 0)
        log(f"  {inst_id} | {signal} | SM:{sm_dir}({sm_str:.2f}) | Score:{score}/12")
        info["inst_id"] = inst_id
        info["price"] = price
        if score > best_score:
            best_score = score
            best_signal = signal
            best_info = info
        await asyncio.sleep(1)
    if best_signal == "WAIT":
        return None, "WAIT", 0, {}
    return best_info.get("inst_id"), best_signal, best_info.get("price", 0), best_info

async def open_trade(session, inst_id, signal, price, info):
    global active_trade, last_trade_time, trades_today, last_trade_day
    margin_1 = current_margin * FIRST_ENTRY_PCT
    position_size = margin_1 * LEVERAGE
    fee_est = position_size * OKX_FEE
    min_sz, ct_val = await get_instrument_info(session, inst_id)
    # Hesabu contracts: position_size / (price * ct_val)
    contracts = position_size / (price * ct_val)
    # Rounddown hadi min_sz
    size = max(round(contracts / min_sz) * min_sz, min_sz)
    log(f"Size calc: position=${position_size:.2f} price={price} ctVal={ct_val} minSz={min_sz} contracts={contracts:.4f} size={size}")
    pos_side = "long" if signal == "LONG" else "short"
    side = "buy" if signal == "LONG" else "sell"
    await set_leverage(session, inst_id, pos_side)
    if not DRY_RUN:
        result = await place_order(session, inst_id, side, pos_side, int(size))
        if not result:
            log("Order imeshindwa")
            return
    if signal == "LONG":
        tp = round(price * (1 + TP_LEVELS[0]), 2)
        sl = round(price * (1 - SL_PCT), 2)
        tps = [round(price * (1 + t), 2) for t in TP_LEVELS]
    else:
        tp = round(price * (1 - TP_LEVELS[0]), 2)
        sl = round(price * (1 + SL_PCT), 2)
        tps = [round(price * (1 - t), 2) for t in TP_LEVELS]
    active_trade = {"inst_id": inst_id, "signal": signal, "pos_side": pos_side,
                    "entry_price": price, "total_margin": margin_1, "position_size": position_size,
                    "tp_price": tp, "sl_price": sl, "tp_idx": 0,
                    "entry_time": time.time(), "added": False, "closed": False, "fee_est": fee_est}
    last_trade_time = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    if last_trade_day != today:
        trades_today = 0
        last_trade_day = today
    trades_today += 1
    coin_name = inst_id.replace("-SWAP", "")
    score = info.get("score", 0)
    sm_dir = info.get("sm_dir", "")
    sm_pct = info.get("sm_str", 0) * 100
    structure = info.get("structure", "")
    mode = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"
    msg = (
        f"🎯 IMEINGIA TRADE!\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 {coin_name}\n"
        f"{'🟢 LONG' if signal == 'LONG' else '🔴 SHORT'}\n"
        f"💲 Entry: {price}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🎯 TP1: {tps[0]} (+{TP_LEVELS[0]*100:.1f}%)\n"
        f"🎯 TP2: {tps[1]} (+{TP_LEVELS[1]*100:.1f}%)\n"
        f"🎯 TP3: {tps[2]} (+{TP_LEVELS[2]*100:.1f}%)\n"
        f"🎯 TP4: {tps[3]} (+{TP_LEVELS[3]*100:.1f}%)\n"
        f"🛑 SL: {sl} (-{SL_PCT*100:.1f}%)\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🧠 SM: {'🟢' if sm_dir=='BULLISH' else '🔴'} {sm_dir} ({sm_pct:.0f}%)\n"
        f"🏗️ Structure: {structure}\n"
        f"📦 OB: {info.get('ob_bull') or info.get('ob_bear') or 'N/A'}\n"
        f"📊 RSI: {info.get('rsi',0):.1f}\n"
        f"📈 EMA50 1H: {info.get('e50_1h',0):.2f}\n"
        f"📉 EMA200 1H: {info.get('e200_1h',0):.2f}\n"
        f"📦 Volume: {'✅' if info.get('vol_ok') else '❌'}\n"
        f"🕯️ Candle: {'✅' if info.get('strong_candle') else '❌'}\n"
        f"⚡ Momentum: {info.get('momentum',0):+.3f}%\n"
        f"🎯 Score: {score}/12\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Margin: ${margin_1:.2f} (30%)\n"
        f"🔢 Leverage: {LEVERAGE}x\n"
        f"📅 Trades leo: {trades_today}/{MAX_TRADES_DAY}\n"
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
            price, _, _ = await get_price(session, trade["inst_id"])
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
                mode2 = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"
                await send_telegram(session, f"➕ IMEONGEZA!\n📊 {coin_name}\n💲 {price}\n💰 +${margin_2:.2f} (20%)\n📈 {change*100:+.2f}%\n⚡ {mode2}")
            tp_hit = (trade["signal"] == "LONG" and price >= trade["tp_price"]) or (trade["signal"] == "SHORT" and price <= trade["tp_price"])
            sl_hit = (trade["signal"] == "LONG" and price <= trade["sl_price"]) or (trade["signal"] == "SHORT" and price >= trade["sl_price"])
            if tp_hit and trade["tp_idx"] < len(TP_LEVELS) - 1:
                cur_pct = TP_LEVELS[trade["tp_idx"]]
                nxt_idx = trade["tp_idx"] + 1
                nxt_pct = TP_LEVELS[nxt_idx]
                entry = trade["entry_price"]
                if trade["signal"] == "LONG":
                    new_tp = round(entry * (1 + nxt_pct), 2)
                    new_sl = round(entry * (1 + cur_pct * 0.5), 2)
                else:
                    new_tp = round(entry * (1 - nxt_pct), 2)
                    new_sl = round(entry * (1 - cur_pct * 0.5), 2)
                trade["tp_price"] = new_tp
                trade["sl_price"] = new_sl
                trade["tp_idx"] = nxt_idx
                mode2 = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"
                await send_telegram(session, f"📈 TP{nxt_idx} IMEFIKIWA!\n📊 {coin_name}\n✅ TP {cur_pct*100:.1f}% imefikiwa\n🎯 TP inayofuata: {new_tp} (+{nxt_pct*100:.1f}%)\n🛡️ SL imehamia: {new_sl}\n📈 {change*100:+.2f}%\n⚡ {mode2}")
                await asyncio.sleep(MONITOR_INTERVAL)
                continue
            if tp_hit or sl_hit:
                if not DRY_RUN:
                    await close_position(session, trade["inst_id"], trade["pos_side"])
                trade["closed"] = True
                gross = change * trade["total_margin"] * LEVERAGE
                fees = trade["fee_est"]
                net = gross - fees
                hold = (time.time() - trade["entry_time"]) / 60
                tp_level = TP_LEVELS[trade["tp_idx"]] * 100
                if net > 0:
                    stats["wins"] += 1
                    emoji = f"💰 TAKE PROFIT! (+{tp_level:.1f}%)"
                else:
                    stats["losses"] += 1
                    emoji = "🛑 STOP LOSS!"
                stats["pnl"] += net
                stats["fees"] += fees
                win_rate = (stats["wins"] / max(stats["wins"] + stats["losses"], 1)) * 100
                balance = await get_futures_balance(session)
                mode = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"
                msg = (f"{emoji}\n━━━━━━━━━━━━━━━\n📊 {coin_name}\n{'🟢 LONG' if trade['signal']=='LONG' else '🔴 SHORT'}\n"
                       f"💲 Entry: {trade['entry_price']}\n💲 Exit: {price}\n⏱️ {hold:.0f} dakika\n"
                       f"━━━━━━━━━━━━━━━\n💵 Gross: {gross:+.4f}\n🏦 Fees: -{fees:.4f}\n✅ Net: {net:+.4f} USDT\n"
                       f"━━━━━━━━━━━━━━━\n📊 Jumla: {stats['pnl']:+.4f}\n🏦 Fees zote: ${stats['fees']:.4f}\n"
                       f"💰 Balance: ${balance:.4f}\n🏆 Win Rate: {win_rate:.0f}% | ✅{stats['wins']} ❌{stats['losses']}\n"
                       f"📅 Trades leo: {trades_today}/{MAX_TRADES_DAY}\n⚡ {mode}")
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
    global active_trade, last_trade_time, trades_today, last_trade_day
    await asyncio.sleep(5)
    while True:
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            if last_trade_day != today:
                trades_today = 0
                last_trade_day = today
            if active_trade and not active_trade["closed"]:
                log(f"⏳ Trade wazi: {active_trade['inst_id']}")
                await asyncio.sleep(SCAN_INTERVAL)
                continue
            if trades_today >= MAX_TRADES_DAY:
                log(f"Limit ya leo: {trades_today}/{MAX_TRADES_DAY}")
                await asyncio.sleep(SCAN_INTERVAL)
                continue
            elapsed = time.time() - last_trade_time
            if last_trade_time > 0 and elapsed < MIN_WAIT:
                log(f"⏳ Inasubiri dakika {int((MIN_WAIT-elapsed)/60)}...")
                await asyncio.sleep(60)
                continue
            inst_id, signal, price, info = await scan_coins(session)
            if not inst_id:
                log("Hakuna signal — inasubiri...")
                await asyncio.sleep(SCAN_INTERVAL)
                continue
            await open_trade(session, inst_id, signal, price, info)
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
        mode = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"
        msg = (f"⚡ BTC/ETH SMART MONEY BOT!\n━━━━━━━━━━━━━━━\n"
               f"💰 Balance: ${balance:.4f} USDT\n🔢 Leverage: {LEVERAGE}x | Isolated\n"
               f"🪙 Coins: BTC + ETH\n━━━━━━━━━━━━━━━\n"
               f"🧠 Smart Money:\n  ✅ Order Book Imbalance\n  ✅ Whale Trade Detection\n"
               f"  ✅ Market Structure (BOS)\n  ✅ Order Block (OB)\n"
               f"  ✅ EMA 50/200 (1H) + EMA 21/50 (5m)\n  ✅ RSI 14 | Volume\n"
               f"  🎯 Min Score: {MIN_SCORE}/12\n━━━━━━━━━━━━━━━\n"
               f"🎯 TP: +0.8% → +1.2% → +1.8% → +2.5%\n"
               f"🛑 SL: -{SL_PCT*100:.1f}% | Entry: 30% | Add: 20%\n"
               f"⏱️ Min kati ya trades: 30 dakika\n⚡ Mode: {mode}")
        log(msg)
        await send_telegram(session, msg)
        await asyncio.gather(scanner_loop(session), monitor_trade(session))

if __name__ == "__main__":
    asyncio.run(main())
