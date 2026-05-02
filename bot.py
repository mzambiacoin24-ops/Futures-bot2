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

# Coins — BTC na ETH peke yake
COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]

DRY_RUN = False
LEVERAGE = 5
FIRST_ENTRY_PCT = 0.30
ADD_ENTRY_PCT = 0.20
ADD_THRESHOLD_PCT = 0.008
OKX_FEE = 0.0005 * 2

# Scalping TP ndogo — faida haraka
TP_LEVELS = [0.008, 0.012, 0.018, 0.025]
SL_PCT = 0.01

MIN_WAIT_BETWEEN_TRADES = 30 * 60  # dakika 30 kati ya trades
MAX_TRADES_PER_DAY = 20
SCAN_INTERVAL = 180  # scan kila dakika 3
MONITOR_INTERVAL = 10
MARGIN_GROWTH_X = 2.0
MARGIN_GROWTH_PCT = 0.50

active_trade = None
stats = {"wins": 0, "losses": 0, "pnl": 0.0, "fees": 0.0}
current_margin = 0.0
last_double_at = 0.0
last_trade_time = 0
trades_today = 0
last_trade_day = ""

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
    return {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }

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
        body = json.dumps({
            "instId": inst_id, "tdMode": "isolated",
            "side": side, "posSide": pos_side,
            "ordType": "market", "sz": str(size),
            "clOrdId": f"bot{int(time.time())}"
        })
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

async def get_candles(session, inst_id, bar="5m", limit=200):
    try:
        path = f"/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            candles = list(reversed(data.get("data", [])))
            opens   = [float(c[1]) for c in candles]
            highs   = [float(c[2]) for c in candles]
            lows    = [float(c[3]) for c in candles]
            closes  = [float(c[4]) for c in candles]
            volumes = [float(c[5]) for c in candles]
            return opens, highs, lows, closes, volumes
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
    """Soma order book — smart money wanaonekana hapa"""
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
    """Soma trades za hivi karibuni — angalia volume kubwa"""
    try:
        path = f"/api/v5/market/trades?instId={inst_id}&limit=50"
        async with session.get(OKX_BASE + path) as r:
            data = await r.json()
            trades = data.get("data", [])
            return trades
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
    """
    Smart Money Concept (SMC):
    1. Order book imbalance — bids kubwa vs asks = bullish
    2. Large trades — whale imeingia upande gani
    3. Bid/Ask ratio — pressure ya soko
    """
    if not bids or not asks:
        return "NEUTRAL", 0.0

    # Hesabu jumla ya bids na asks (top 10)
    bid_volume = sum(b[1] for b in bids[:10])
    ask_volume = sum(a[1] for a in asks[:10])

    total = bid_volume + ask_volume
    if total == 0:
        return "NEUTRAL", 0.0

    bid_ratio = bid_volume / total

    # Angalia trades kubwa za hivi karibuni (whale trades)
    buy_vol = 0.0
    sell_vol = 0.0
    for t in trades[:30]:
        sz = float(t.get("sz", 0))
        side = t.get("side", "")
        if side == "buy":
            buy_vol += sz
        elif side == "sell":
            sell_vol += sz

    trade_total = buy_vol + sell_vol
    trade_buy_ratio = buy_vol / trade_total if trade_total > 0 else 0.5

    # Combining: order book + trade flow
    combined = (bid_ratio * 0.5) + (trade_buy_ratio * 0.5)

    if combined > 0.62:
        return "BULLISH", combined
    elif combined < 0.38:
        return "BEARISH", combined
    return "NEUTRAL", combined

def detect_structure(highs, lows, closes):
    """
    Market Structure:
    - Higher Highs + Higher Lows = UPTREND (BOS up)
    - Lower Highs + Lower Lows = DOWNTREND (BOS down)
    - CHoCH = Change of Character — trend inabadilika
    """
    if len(closes) < 20:
        return "UNKNOWN"

    # Angalia swing highs na lows (last 20 candles)
    recent_highs = highs[-20:]
    recent_lows = lows[-20:]

    # Higher Highs
    hh = recent_highs[-1] > recent_highs[-10]
    # Higher Lows
    hl = recent_lows[-1] > recent_lows[-10]
    # Lower Highs
    lh = recent_highs[-1] < recent_highs[-10]
    # Lower Lows
    ll = recent_lows[-1] < recent_lows[-10]

    if hh and hl:
        return "UPTREND"
    elif lh and ll:
        return "DOWNTREND"
    return "RANGING"

def detect_ob_fvg(opens, highs, lows, closes):
    """
    Order Block (OB) na Fair Value Gap (FVG):
    - OB: Candle kubwa kabla ya move — smart money wanabuy/sell hapa
    - FVG: Gap kati ya candles 3 — bei inarudi hapa kujaza
    """
    if len(closes) < 5:
        return None, None, None, None

    ob_bull = None
    ob_bear = None
    fvg_bull = None
    fvg_bear = None

    # Order Block — tafuta candle kubwa (body > 60% ya candle)
    for i in range(len(closes) - 5, len(closes) - 1):
        body = abs(closes[i] - opens[i])
        candle_range = highs[i] - lows[i]
        if candle_range == 0:
            continue
        body_ratio = body / candle_range

        if body_ratio > 0.6:
            if closes[i] > opens[i]:  # Bullish OB
                ob_bull = lows[i]
            elif closes[i] < opens[i]:  # Bearish OB
                ob_bear = highs[i]

    # Fair Value Gap — gap kati ya candle 1 na candle 3
    for i in range(len(closes) - 4, len(closes) - 1):
        if i < 1:
            continue
        # Bullish FVG: low ya candle 3 > high ya candle 1
        if lows[i+1] > highs[i-1]:
            fvg_bull = (highs[i-1] + lows[i+1]) / 2
        # Bearish FVG: high ya candle 3 < low ya candle 1
        if highs[i+1] < lows[i-1]:
            fvg_bear = (lows[i-1] + highs[i+1]) / 2

    return ob_bull, ob_bear, fvg_bull, fvg_bear

def analyze_smart(opens, highs, lows, closes, volumes,
                  opens_1h, highs_1h, lows_1h, closes_1h,
                  sm_direction, sm_strength, high24h, low24h):
    """
    Smart Money Analysis:
    1. Market Structure (BOS/CHoCH)
    2. Order Block presence
    3. Fair Value Gap
    4. Smart Money direction (order book + trades)
    5. EMA 50/200 trend
    6. RSI 14
    7. Volume confirmation
    """
    if len(closes) < 50 or len(closes_1h) < 50:
        return "WAIT", {}

    price = closes[-1]

    # === EMA ya 5m ===
    e21_5m = ema(closes, 21)
    e50_5m = ema(closes, 50)

    # === EMA ya 1H ===
    e50_1h = ema(closes_1h, 50)
    e200_1h = ema(closes_1h, 200)

    # === RSI 5m ===
    r = rsi(closes, 14)

    # === Market Structure ===
    structure = detect_structure(highs_1h, lows_1h, closes_1h)

    # === Order Block na FVG ===
    ob_bull, ob_bear, fvg_bull, fvg_bear = detect_ob_fvg(opens, highs, lows, closes)

    # === Volume ===
    vol_ok = False
    if len(volumes) >= 10:
        vol_ok = sum(volumes[-3:]) / 3 > sum(volumes[-10:-3]) / 7 * 1.4

    # === Candle strength ===
    candle_body = abs(closes[-1] - opens[-1])
    candle_range = highs[-1] - lows[-1]
    strong_candle = (candle_body / candle_range > 0.5) if candle_range > 0 else False

    # === Momentum 5m ===
    momentum = (closes[-1] - closes[-4]) / closes[-4] * 100 if closes[-4] > 0 else 0

    # === 24h position ===
    range_24h = high24h - low24h
    price_pos = (price - low24h) / range_24h * 100 if range_24h > 0 else 50

    info = {
        "rsi": r,
        "e21_5m": e21_5m,
        "e50_5m": e50_5m,
        "e50_1h": e50_1h,
        "e200_1h": e200_1h,
        "structure": structure,
        "sm_direction": sm_direction,
        "sm_strength": sm_strength,
        "ob_bull": ob_bull,
        "ob_bear": ob_bear,
        "fvg_bull": fvg_bull,
        "fvg_bear": fvg_bear,
        "vol_ok": vol_ok,
        "strong_candle": strong_candle,
        "momentum": momentum,
        "price_pos": price_pos,
        "high24h": high24h,
        "low24h": low24h,
        "price": price
    }

    # ================================================================
    # LONG CONDITIONS — Smart Money + Structure + Indicators
    # ================================================================
    if sm_direction == "BULLISH" and structure == "UPTREND":
        long_score = sum([
            sm_direction == "BULLISH",           # Smart money ni bullish
            sm_strength > 0.60,                  # Nguvu ya SM > 60%
            structure == "UPTREND",               # Market structure ni uptrend
            e50_1h > e200_1h,                    # 1H trend ni bullish
            price > e50_1h,                      # Bei iko juu ya EMA50 1H
            e21_5m > e50_5m,                     # 5m EMA cross bullish
            50 < r < 70,                         # RSI bullish zone
            vol_ok,                              # Volume inaongezeka
            strong_candle,                       # Candle yenye nguvu
            momentum > 0.05,                     # Momentum chanya
            price_pos < 60,                      # Bei si karibu sana na high
            ob_bull is not None,                 # Kuna bullish OB
        ])
        info["score"] = long_score
        info["direction"] = "LONG"
        if long_score >= 8:
            return "LONG", info

    # ================================================================
    # SHORT CONDITIONS — Smart Money + Structure + Indicators
    # ================================================================
    if sm_direction == "BEARISH" and structure == "DOWNTREND":
        short_score = sum([
            sm_direction == "BEARISH",           # Smart money ni bearish
            sm_strength < 0.40,                  # Nguvu ya SM < 40%
            structure == "DOWNTREND",             # Market structure ni downtrend
            e50_1h < e200_1h,                    # 1H trend ni bearish
            price < e50_1h,                      # Bei iko chini ya EMA50 1H
            e21_5m < e50_5m,                     # 5m EMA cross bearish
            30 < r < 50,                         # RSI bearish zone
            vol_ok,                              # Volume inaongezeka
            strong_candle,                       # Candle yenye nguvu
            momentum < -0.05,                    # Momentum hasi
            price_pos > 40,                      # Bei si karibu sana na low
            ob_bear is not None,                 # Kuna bearish OB
        ])
        info["score"] = short_score
        info["direction"] = "SHORT"
        if short_score >= 8:
            return "SHORT", info

    return "WAIT", info

async def analyze_coin(session, inst_id):
    """Chunguza coin moja kwa kina"""
    # Candles za 5m (scalping)
    opens_5m, highs_5m, lows_5m, closes_5m, volumes_5m = await get_candles(session, inst_id, "5m", 100)
    if len(closes_5m) < 50:
        return "WAIT", {}

    # Candles za 1H (trend)
    opens_1h, highs_1h, lows_1h, closes_1h, _ = await get_candles(session, inst_id, "1H", 220)
    if len(closes_1h) < 50:
        return "WAIT", {}

    # Order book na trades (smart money)
    bids, asks = await get_orderbook(session, inst_id)
    trades = await get_trades(session, inst_id)
    sm_direction, sm_strength = detect_smart_money(bids, asks, trades)

    # Bei ya sasa
    price, high24h, low24h = await get_price(session, inst_id)
    if not price:
        return "WAIT", {}

    signal, info = analyze_smart(
        opens_5m, highs_5m, lows_5m, closes_5m, volumes_5m,
        opens_1h, highs_1h, lows_1h, closes_1h,
        sm_direction, sm_strength, high24h, low24h
    )

    info["inst_id"] = inst_id
    info["price"] = price
    return signal, info

async def scan_coins(session):
    """Scan BTC na ETH — chagua bora"""
    best_signal = "WAIT"
    best_info = {}
    best_score = 0

    for inst_id in COINS:
        log(f"🔍 Inachunguza {inst_id}...")
        signal, info = await analyze_coin(session, inst_id)

        if signal == "WAIT":
            log(f"  ⏭️ {inst_id} — Hakuna signal")
            await asyncio.sleep(1)
            continue

        score = info.get("score", 0)
        sm = info.get("sm_direction", "")
        structure = info.get("structure", "")
        log(f"  ✅ {inst_id} | {signal} | SM:{sm} | Structure:{structure} | Score:{score}/12")

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
    size = max(round(position_size / (price * ct_val), 0), min_sz)
    pos_side = "long" if signal == "LONG" else "short"
    side = "buy" if signal == "LONG" else "sell"

    await set_leverage(session, inst_id, pos_side)

    if not DRY_RUN:
        result = await place_order(session, inst_id, side, pos_side, int(size))
        if not result:
            log("Order imeshindwa")
            return

    first_tp_pct = TP_LEVELS[0]
    if signal == "LONG":
        tp = round(price * (1 + first_tp_pct), 2)
        sl = round(price * (1 - SL_PCT), 2)
        tp1 = round(price * (1 + TP_LEVELS[0]), 2)
        tp2 = round(price * (1 + TP_LEVELS[1]), 2)
        tp3 = round(price * (1 + TP_LEVELS[2]), 2)
        tp4 = round(price * (1 + TP_LEVELS[3]), 2)
    else:
        tp = round(price * (1 - first_tp_pct), 2)
        sl = round(price * (1 + SL_PCT), 2)
        tp1 = round(price * (1 - TP_LEVELS[0]), 2)
        tp2 = round(price * (1 - TP_LEVELS[1]), 2)
        tp3 = round(price * (1 - TP_LEVELS[2]), 2)
        tp4 = round(price * (1 - TP_LEVELS[3]), 2)

    active_trade = {
        "inst_id": inst_id, "signal": signal, "pos_side": pos_side,
        "entry_price": price, "total_margin": margin_1,
        "position_size": position_size, "tp_price": tp, "sl_price": sl,
        "tp_idx": 0, "entry_time": time.time(),
        "added": False, "closed": False, "fee_est": fee_est
    }

    last_trade_time = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    if last_trade_day != today:
        trades_today = 0
        last_trade_day = today
    trades_today += 1

    coin_name = inst_id.replace("-SWAP", "")
    score = info.get("score", 0)
    sm = info.get("sm_direction", "")
    sm_pct = info.get("sm_strength", 0) * 100
    structure = info.get("structure", "")
    mode = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"

    ob_info = ""
    if signal == "LONG" and info.get("ob_bull"):
        ob_info = f"📦 Order Block: ${info['ob_bull']:.2f}\n"
    elif signal == "SHORT" and info.get("ob_bear"):
        ob_info = f"📦 Order Block: ${info['ob_bear']:.2f}\n"

    fvg_info = ""
    if signal == "LONG" and info.get("fvg_bull"):
        fvg_info = f"📊 FVG: ${info['fvg_bull']:.2f}\n"
    elif signal == "SHORT" and info.get("fvg_bear"):
        fvg_info = f"📊 FVG: ${info['fvg_bear']:.2f}\n"

    msg = (
        f"🎯 IMEINGIA TRADE!\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 {coin_name}\n"
        f"{'🟢 LONG' if signal == 'LONG' else '🔴 SHORT'}\n"
        f"💲 Entry: {price}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🎯 TP1: {tp1} (+{TP_LEVELS[0]*100:.1f}%)\n"
        f"🎯 TP2: {tp2} (+{TP_LEVELS[1]*100:.1f}%)\n"
        f"🎯 TP3: {tp3} (+{TP_LEVELS[2]*100:.1f}%)\n"
        f"🎯 TP4: {tp4} (+{TP_LEVELS[3]*100:.1f}%)\n"
        f"🛑 SL: {sl} (-{SL_PCT*100:.1f}%)\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🧠 SMART MONEY:\n"
        f"{'🟢' if sm == 'BULLISH' else '🔴'} Direction: {sm} ({sm_pct:.0f}%)\n"
        f"🏗️ Structure: {structure}\n"
        f"{ob_info}"
        f"{fvg_info}"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 RSI 14: {info.get('rsi', 0):.1f}\n"
        f"📈 EMA50 1H: {info.get('e50_1h', 0):.2f}\n"
        f"📉 EMA200 1H: {info.get('e200_1h', 0):.2f}\n"
        f"📦 Volume: {'✅' if info.get('vol_ok') else '❌'}\n"
        f"🕯️ Candle: {'✅' if info.get('strong_candle') else '❌'}\n"
        f"⚡ Momentum: {info.get('momentum', 0):+.3f}%\n"
        f"🎯 Score: {score}/12\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Margin: ${margin_1:.2f} (30%)\n"
        f"🔢 Leverage: {LEVERAGE}x\n"
        f"📅 Trades leo: {trades_today}/{MAX_TRADES_PER_DAY}\n"
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

            if trade["signal"] == "LONG":
                change = (price - trade["entry_price"]) / trade["entry_price"]
            else:
                change = (trade["entry_price"] - price) / trade["entry_price"]

            # Ongeza position ikiendelea vizuri
            if not trade["added"] and change >= ADD_THRESHOLD_PCT:
                margin_2 = current_margin * ADD_ENTRY_PCT
                position_2 = margin_2 * LEVERAGE
                min_sz, ct_val = await get_instrument_info(session, trade["inst_id"])
                size_2 = max(round(position_2 / (price * ct_val), 0), min_sz)
                if not DRY_RUN:
                    side = "buy" if trade["signal"] == "LONG" else "sell"
                    await place_order(session, trade["inst_id"], side, trade["pos_side"], int(size_2))
                trade["total_margin"] += margin_2
                trade["position_size"] += position_2
                trade["fee_est"] += position_2 * OKX_FEE
                trade["added"] = True
                mode2 = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"
                await send_telegram(session,
                    f"➕ IMEONGEZA!\n"
                    f"📊 {coin_name}\n"
                    f"💲 Bei: {price}\n"
                    f"💰 +${margin_2:.2f} (20%)\n"
                    f"📈 {change*100:+.2f}%\n"
                    f"⚡ {mode2}"
                )

            tp_hit = (
                (trade["signal"] == "LONG" and price >= trade["tp_price"]) or
                (trade["signal"] == "SHORT" and price <= trade["tp_price"])
            )
            sl_hit = (
                (trade["signal"] == "LONG" and price <= trade["sl_price"]) or
                (trade["signal"] == "SHORT" and price >= trade["sl_price"])
            )

            # TP imefikiwa — hamisha SL, subiri inayofuata
            if tp_hit and trade["tp_idx"] < len(TP_LEVELS) - 1:
                current_tp_pct = TP_LEVELS[trade["tp_idx"]]
                next_idx = trade["tp_idx"] + 1
                next_tp_pct = TP_LEVELS[next_idx]
                entry = trade["entry_price"]

                if trade["signal"] == "LONG":
                    new_tp = round(entry * (1 + next_tp_pct), 2)
                    new_sl = round(entry * (1 + current_tp_pct * 0.5), 2)
                else:
                    new_tp = round(entry * (1 - next_tp_pct), 2)
                    new_sl = round(entry * (1 - current_tp_pct * 0.5), 2)

                trade["tp_price"] = new_tp
                trade["sl_price"] = new_sl
                trade["tp_idx"] = next_idx

                mode2 = "🔴 LIVE" if not DRY_RUN else "🧪 SIM"
                await send_telegram(session,
                    f"📈 TP{next_idx} IMEFIKIWA!\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📊 {coin_name}\n"
                    f"✅ TP {current_tp_pct*100:.1f}% imefikiwa\n"
                    f"🎯 TP inayofuata: {new_tp} (+{next_tp_pct*100:.1f}%)\n"
                    f"🛡️ SL imehamia: {new_sl} (faida imelindwa!)\n"
                    f"📈 Faida sasa: {change*100:+.2f}%\n"
                    f"⚡ {mode2}"
                )
                await asyncio.sleep(MONITOR_INTERVAL)
                continue

            # Funga trade
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
                    f"📅 Trades leo: {trades_today}/{MAX_TRADES_PER_DAY}\n"
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
        await send_telegram(session,
            f"📈 MARGIN IMEONGEZWA!\n"
            f"💰 Balance: ${balance:.4f}\n"
            f"📊 ${old:.2f} → ${current_margin:.2f}\n"
            f"🎯 Itaongezwa: ${balance * MARGIN_GROWTH_X:.2f}"
        )

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

            if trades_today >= MAX_TRADES_PER_DAY:
                log(f"🛑 Limit ya leo: {trades_today}/{MAX_TRADES_PER_DAY}")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            elapsed = time.time() - last_trade_time
            if last_trade_time > 0 and elapsed < MIN_WAIT_BETWEEN_TRADES:
                wait_left = int((MIN_WAIT_BETWEEN_TRADES - elapsed) / 60)
                log(f"⏳ Inasubiri dakika {wait_left}...")
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
        msg = (
            f"⚡ BTC/ETH SMART MONEY BOT!\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 Balance: ${balance:.4f} USDT\n"
            f"🔢 Leverage: {LEVERAGE}x | Isolated\n"
            f"🪙 Coins: BTC + ETH peke yake\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🧠 Smart Money Analysis:\n"
            f"  ✅ Order Book Imbalance\n"
            f"  ✅ Whale Trade Detection\n"
            f"  ✅ Market Structure (BOS/CHoCH)\n"
            f"  ✅ Order Block (OB)\n"
            f"  ✅ Fair Value Gap (FVG)\n"
            f"  ✅ EMA 50/200 1H\n"
            f"  ✅ RSI 14 | Volume\n"
            f"  🎯 Min Score: 8/12\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🎯 Scalping TP:\n"
            f"  TP1: +0.8% | TP2: +1.2%\n"
            f"  TP3: +1.8% | TP4: +2.5%\n"
            f"🛑 SL: -{SL_PCT*100:.1f}%\n"
            f"💰 Entry: 30% | Add: 20%\n"
            f"⏱️ Min kati ya trades: 30 dakika\n"
            f"⚡ Mode: {mode}"
        )
        log(msg)
        await send_telegram(session, msg)
        await asyncio.gather(scanner_loop(session), monitor_trade(session))

if __name__ == "__main__":
    asyncio.run(main())
