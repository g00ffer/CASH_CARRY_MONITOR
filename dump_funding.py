import asyncio
import json

import ccxt.async_support as ccxt


async def dump():
    ex = ccxt.binance({"options": {"defaultType": "swap"}})
    try:
        await ex.load_markets()
        for symbol in ("BTC/USDT:USDT", "ETH/USDT:USDT"):
            raw = await ex.fetch_funding_rate(symbol)
            print(f"\n{'=' * 60}")
            print(f"SYMBOL: {symbol}")
            print(f"{'=' * 60}")
            print(json.dumps(raw, indent=2, default=str))
    finally:
        await ex.close()


asyncio.run(dump())