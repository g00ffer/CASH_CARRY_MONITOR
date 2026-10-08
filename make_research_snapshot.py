#!/usr/bin/env python3
"""
Лёгкий research-снапшот баз для хранения в git.
Принципы:
 - без payload и market_snapshots (основной объём, не нужен для анализа)
 - funding: одна строка на settlement (dedup по next_funding_timestamp_ms)
 - metrics / signal_decisions: 5-минутные бакеты
 - alerts: без текстов сообщений
 - klines 1h: как есть (маленькие)
Выход: research_snapshots/research_YYYY-MM-DD.sqlite.gz (единицы МБ)
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATA = Path("data")
OUT_DIR = Path("research_snapshots")
B = 5 * 60 * 1000  # 5-минутный бакет, ms


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = OUT_DIR / f"research_{stamp}.sqlite"
    if out.exists():
        out.unlink()

    dst = sqlite3.connect(f"file:{out}?mode=rwc", uri=True)
    dst.execute(
        f"ATTACH DATABASE 'file:{DATA / 'monitor.sqlite'}?mode=ro' AS m"
    )
    dst.execute(
        f"ATTACH DATABASE 'file:{DATA / 'bybit_funding.sqlite'}?mode=ro' AS b"
    )
    has_klines = (DATA / "klines.sqlite").exists()
    if has_klines:
        dst.execute(
            f"ATTACH DATABASE 'file:{DATA / 'klines.sqlite'}?mode=ro' AS k"
        )

    dst.execute(
        """
        CREATE TABLE metrics_5m AS
        SELECT symbol_name,
               (calculated_at_ms / :b) * :b AS bucket_ms,
               AVG(CAST(basis_entry    AS REAL)) AS basis_entry,
               AVG(CAST(funding_annual AS REAL)) AS funding_annual,
               AVG(CAST(net_horizon    AS REAL)) AS net_horizon,
               AVG(CAST(net_annual     AS REAL)) AS net_annual
        FROM m.metrics GROUP BY 1, 2
        """,
        {"b": B},
    )
    dst.execute(
        """
        CREATE TABLE funding_settlements AS
        SELECT symbol_name,
               next_funding_timestamp_ms AS settle_ms,
               CAST(effective_funding_rate AS REAL) AS rate
        FROM (
            SELECT symbol_name, next_funding_timestamp_ms,
                   effective_funding_rate,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol_name, next_funding_timestamp_ms
                       ORDER BY received_at_ms DESC
                   ) rn
            FROM m.funding_snapshots
            WHERE next_funding_timestamp_ms IS NOT NULL
        )
        WHERE rn = 1
        """,
    )
    dst.execute(
        """
        CREATE TABLE signal_decisions_5m AS
        SELECT symbol_name,
               (decision_timestamp_ms / :b) * :b AS bucket_ms,
               state,
               MAX(should_alert) AS should_alert,
               MAX(consecutive_confirmations) AS conf
        FROM m.signal_decisions GROUP BY 1, 2, 3
        """,
        {"b": B},
    )
    dst.execute(
        """
        CREATE TABLE alerts AS
        SELECT symbol_name, alert_type, delivery_status,
               created_at_ms, sent_at_ms, error_message
        FROM m.alerts
        """,
    )
    dst.execute(
        """
        CREATE TABLE funding_settlements_bybit AS
        SELECT symbol_name,
               next_funding_timestamp_ms AS settle_ms,
               funding_rate AS rate
        FROM (
            SELECT symbol_name, next_funding_timestamp_ms, funding_rate,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol_name, next_funding_timestamp_ms
                       ORDER BY received_at_ms DESC
                   ) rn
            FROM b.funding_bybit
            WHERE next_funding_timestamp_ms IS NOT NULL
        )
        WHERE rn = 1
        """,
    )
    if has_klines:
        dst.execute("CREATE TABLE klines_1h AS SELECT * FROM k.klines")

    dst.commit()
    dst.close()

    gz = out.with_suffix(".sqlite.gz")
    with open(out, "rb") as f_in, gzip.open(gz, "wb", compresslevel=9) as f_out:
        shutil.copyfileobj(f_in, f_out)
    out.unlink()
    print(f"✅ {gz} ({gz.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
