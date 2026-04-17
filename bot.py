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
TP_PCT = 0.02
SL_PCT = 0.012
ADD_THRESHOLD_PCT = 0.01

MIN_VOLUME_USD = 2_000_000
MIN_MOMENTUM_PCT = 1.0
MAX_MOMENTUM_PCT = 8.0
SCAN_INTERVAL = 300
MONITOR_INTERVAL = 10

active_trade = None
stats = {"wins": 0, "losses": 0, "pnl": 0.0}

base_capital = 0.0
current_margin = 0.0
last_double_at = 0.0

def log(msg):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}")

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

async def set_leverage(session, inst_id):
    path = "/api/v5/account/set-leverage"
    body = json.dumps({
        "instId": inst_id,
        "lever": str(LEVERAGE),
        "mgnMode": "cross"
    })
    headers = get_headers("POST", path, body)
    async with session.post(OKX_BASE + path, headers=headers, data=body) as r:
        await r.json()

async def place_futures_order(session, inst_id, side, size):
    try:
        path = "/api/v5/trade/order"

        body = json.dumps({
            "instId": inst_id,
            "tdMode": "cross",
            "side": side,
            "ordType": "market",
            "sz": str(size)
        })

        headers = get_headers("POST", path, body)

        async with session.post(OKX_BASE + path, headers=headers, data=body) as r:
            data = await r.json()

            if data.get("code") == "0":
                return data.get("data", [{}])[0]
            else:
                log(f"FULL ERROR: {data}")
                return None

    except Exception as e:
        log(f"Order error: {e}")
        return None

async def close_futures_order(session, inst_id, signal):
    try:
        path = "/api/v5/trade/order"
        side = "sell" if signal == "LONG" else "buy"

        body = json.dumps({
            "instId": inst_id,
            "tdMode": "cross",
            "side": side,
            "ordType": "market",
            "reduceOnly": True
        })

        headers = get_headers("POST", path, body)

        async with session.post(OKX_BASE + path, headers=headers, data=body) as r:
            data = await r.json()
            return data.get("code") == "0"

    except Exception as e:
        log(f"Close error: {e}")
        return False

async def open_trade(session, inst_id, signal, price):
    margin = current_margin * FIRST_ENTRY_PCT
    size = max(1, int((margin * LEVERAGE) / price))

    await set_leverage(session, inst_id)

    side = "buy" if signal == "LONG" else "sell"
    result = await place_futures_order(session, inst_id, side, size)

    if not result:
        log("Order imeshindwa!")
        return

    log(f"ORDER SUCCESS: {inst_id} {side} size={size}")

async def main():
    async with aiohttp.ClientSession() as session:
        while True:
            await open_trade(session, "BTC-USDT-SWAP", "LONG", 60000)
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
