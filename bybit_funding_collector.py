#!/usr/bin/env python3
"""
Bybit funding collector (Stage 3 research).

Polls Bybit public API (no auth) every 60s and stores perp funding
rates into a SEPARATE SQLite (data/bybit_funding.sqlite) to avoid
lock contention with the main monitor DB.

Purpose: collect Binance↔Bybit funding spread data for
backtest_v4_cross_exchange.py.

Run: python bybit_funding_collector.py
(or via systemd, see below)
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

DB_PATH = Path("data/bybit_funding.sqlite")
POLL_INTERVAL_SEC = 60
BYBIT_URL = "https://api.bybit.com/v5/market/tickers"

# Bybit linear symbol -> internal name (same as symbols.yaml)
SYMBOL_MAP: dict[str, str] = {
    "BTCUSDT": "BTC_CARRY",
    "ETHUSDT": "ETH_CARRY",
    "SOLUSDT": "SOL_CARRY",
    "BNBUSDT": "BNB_CARRY",
    "XRPUSDT": "XRP_CARRY",
    "DOGEUSDT": "DOGE_CARRY",
    "ADAUSDT": "ADA_CARRY",
    "AVAXUSDT": "AVAX_CARRY",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("bybit_collector")


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS funding_bybit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol_name TEXT NOT NULL,
            bybit_symbol TEXT NOT NULL,
            received_at_ms INTEGER NOT NULL,
            funding_rate REAL,
            next_funding_timestamp_ms INTEGER,
            mark_price REAL,
            index_price REAL,
            last_price REAL,
            payload TEXT,
            created_at_ms INTEGER NOT NULL
        )
        """,
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_funding_bybit_sym_time
        ON funding_bybit(symbol_name, received_at_ms)
        """,
    )
    conn.commit()


async def fetch_tickers(client: httpx.AsyncClient) -> list[dict]:
    """One request for ALL linear tickers, then filter locally."""
    for attempt in range(3):
        try:
            resp = await client.get(
                BYBIT_URL, params={"category": "linear"},
            )
            resp.raise_for_status()
            body = resp.json()
            if body.get("retCode") != 0:
                raise RuntimeError(
                    f"Bybit retCode={body.get('retCode')}: "
                    f"{body.get('retMsg')}",
                )
            return body["result"]["list"]
        except Exception as exc:
            logger.warning(
                "fetch attempt %d failed: %s", attempt + 1, exc,
            )
            await asyncio.sleep(2 * (attempt + 1))
    return []


def store_rows(conn: sqlite3.Connection, tickers: list[dict]) -> int:
    now_ms = utc_now_ms()
    rows = []
    for t in tickers:
        bybit_symbol = t.get("symbol", "")
        symbol_name = SYMBOL_MAP.get(bybit_symbol)
        if symbol_name is None:
            continue
        try:
            rows.append((
                symbol_name,
                bybit_symbol,
                now_ms,
                float(t.get("fundingRate") or 0.0),
                int(t.get("nextFundingTime") or 0),
                float(t.get("markPrice") or 0.0),
                float(t.get("indexPrice") or 0.0),
                float(t.get("lastPrice") or 0.0),
                json.dumps(t),
                now_ms,
            ))
        except (TypeError, ValueError) as exc:
            logger.warning("parse error for %s: %s", bybit_symbol, exc)
    if rows:
        conn.executemany(
            """
            INSERT INTO funding_bybit (
                symbol_name, bybit_symbol, received_at_ms,
                funding_rate, next_funding_timestamp_ms,
                mark_price, index_price, last_price,
                payload, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    return len(rows)


async def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    init_db(conn)
    logger.info(
        "Bybit collector started, db=%s, symbols=%s",
        DB_PATH, list(SYMBOL_MAP),
    )

    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            started = time.monotonic()
            tickers = await fetch_tickers(client)
            if tickers:
                n = store_rows(conn, tickers)
                logger.info("stored %d rows", n)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, POLL_INTERVAL_SEC - elapsed))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("collector stopped")