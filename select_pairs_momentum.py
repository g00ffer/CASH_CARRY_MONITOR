#!/usr/bin/env python3
"""
select_pairs_momentum.py — Асинхронный отбор пар для momentum-стратегии через Bybit API.

Архитектура (по аналогии с select_pairs.py):
1. Все активные USDT-перпетуалы с Bybit (~447)
2. Фильтр по ликвидности (volume 24h)
3. BTC health check (глобальный рыночный фильтр)
4. Trend metrics: Hurst, VR720, Vol, Corr(BTC)
5. Корреляционный фильтр между отобранными парами (убираем дубликаты)
6. Атомарная запись whitelist_momentum.json
7. Отчёт в Telegram (только количество пар)

Запуск: раз в день через cron.
"""
import os
import json
import time
import math
import logging
import tempfile
import traceback
from datetime import datetime
import pandas as pd
import numpy as np
import ccxt.async_support as ccxt
import asyncio
import aiohttp

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ---------- НАСТРОЙКИ ----------
MIN_VOLUME_USDT_24H = 5_000_000   # $5M минимум за 24h
LOOKBACK_1H = 800                  # ~33 дня часовых свечей

# Trend criteria
MIN_HURST = 0.55
MIN_VR720 = 1.0
MIN_VOL = 0.40
MAX_VOL = 1.50
MAX_CORR_BTC = 0.85

# Корреляционный фильтр между парами
MAX_PAIR_CORR = 0.75

# BTC health
BTC_SMA20_MAX_DROP = 0.95
BTC_3H_MAX_DROP = -4.0

# Пути
USER_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.')
OUTPUT_FILE = os.path.join(USER_DATA_DIR, 'whitelist_momentum.json')


# ---------- АТОМАРНАЯ ЗАПИСЬ ----------
def atomic_json_write(filepath: str, data: dict) -> None:
    """Атомарная запись JSON: tempfile + os.replace (POSIX-атомарно)."""
    dir_name = os.path.dirname(os.path.abspath(filepath))
    fd, tmp_path = tempfile.mkstemp(suffix='.tmp', dir=dir_name)
    try:
        with os.fdopen(fd, 'w') as tmp:
            json.dump(data, tmp, indent=4)
        os.replace(tmp_path, filepath)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


# ---------- TREND METRICS ----------
def hurst_exponent(ts: np.ndarray) -> float:
    """
    Упрощённый R/S анализ.
    H > 0.5 = трендовость, H < 0.5 = mean-reversion.
    """
    n = len(ts)
    if n < 200:
        return 0.5
    points = []
    for k in [16, 32, 64, 128, 256, 512]:
        if k > n // 4:
            continue
        rs_vals = []
        for b in range(n // k):
            block = ts[b * k:(b + 1) * k]
            mean = np.mean(block)
            dev = np.cumsum(block - mean)
            R = np.max(dev) - np.min(dev)
            S = np.std(block)
            if S > 1e-12:
                rs_vals.append(R / S)
        if rs_vals:
            points.append((math.log(k), math.log(np.mean(rs_vals))))
    if len(points) < 2:
        return 0.5
    x = np.array([p[0] for p in points])
    y = np.array([p[1] for p in points])
    return float(np.polyfit(x, y, 1)[0])


def variance_ratio(ts: np.ndarray, q: int) -> float:
    """
    Variance Ratio на масштабе q часов.
    VR > 1 = трендовость на масштабе q (положительная автокорреляция).
    VR < 1 = mean-reversion.
    """
    n = len(ts)
    if n < q * 4:
        return 1.0
    rq = np.array([ts[i:i + q].sum() for i in range(0, n - q, q)])
    v1 = np.var(ts, ddof=1)
    vq = np.var(rq, ddof=1)
    if v1 <= 1e-15:
        return 1.0
    return float(vq / (q * v1))


def realized_vol(ts: np.ndarray) -> float:
    """Годовая реализованная волатильность из часовых доходностей."""
    if len(ts) < 2:
        return 0.0
    return float(np.std(ts) * math.sqrt(365.25 * 24))


# ---------- BYBIT API ----------
async def get_active_usdt_pairs(exchange) -> list:
    """Все активные USDT-перпетуалы (linear swap) с Bybit."""
    STABLECOINS = {'USDC', 'DAI', 'BUSD', 'TUSD', 'USDP', 'USDD',
                   'USTC', 'PAX', 'GUSD', 'USDJ', 'USDS'}
    try:
        await exchange.load_markets()
        pairs = []
        for symbol, market in exchange.markets.items():
            if not market.get('active'):
                continue
            # Только linear swap (perpetual futures), не spot
            if not market.get('swap'):
                continue
            if not market.get('linear'):
                continue
            if market.get('quote') != 'USDT':
                continue
            base = market.get('base', '')
            if base in STABLECOINS:
                continue
            if any(x in symbol for x in ['3L', '3S', '2L', '2S', '5L', '5S']):
                continue
            pairs.append(symbol)
        return pairs
    except Exception as e:
        logger.error(f"Ошибка получения рынков: {type(e).__name__}: {e}")
        logger.error(f"Traceback:\n{traceback.format_exc()}")
        return []


async def fetch_candles(exchange, pair, timeframe='1h', limit=800):
    """Загрузка свечей через ccxt."""
    try:
        ohlcv = await exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=limit)
        if not ohlcv:
            return None
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = df.sort_values('timestamp').reset_index(drop=True)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize(None)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        logger.error(f"Ошибка загрузки свечей {pair} {timeframe}: {e}")
        return None


async def check_btc_health(exchange) -> tuple:
    """
    Проверка здоровья BTC. Возвращает (healthy, reason).
    Блокирует отбор при общерыночном обвале.
    """
    btc_1h = await fetch_candles(exchange, 'BTC/USDT:USDT', '1h', limit=50)
    if btc_1h is None or len(btc_1h) < 20:
        return False, "нет данных BTC"

    btc_close = btc_1h['close'].iloc[-1]
    btc_sma20 = btc_1h['close'].rolling(20).mean().iloc[-1]

    if pd.isna(btc_sma20) or btc_sma20 <= 0:
        return False, "SMA20 BTC не рассчитана"

    # BTC ниже SMA20 более чем на 5% — медвежий рынок
    if btc_close < btc_sma20 * BTC_SMA20_MAX_DROP:
        drop = (1 - btc_close / btc_sma20) * 100
        return False, f"BTC ниже SMA20 на {drop:.1f}%"

    # Падение BTC за 3 часа > 4% — паника
    if len(btc_1h) >= 4:
        btc_3h_change = (btc_close / btc_1h['close'].iloc[-4] - 1) * 100
        if btc_3h_change < BTC_3H_MAX_DROP:
            return False, f"BTC упал на {btc_3h_change:.1f}% за 3ч"

    return True, "OK"


# ---------- ОТБОР ПАР ----------
async def filter_pairs(exchange):
    """Многоступенчатый отбор пар."""
    all_pairs = await get_active_usdt_pairs(exchange)
    logger.info(f"Найдено {len(all_pairs)} активных USDT-перпетуалов.")

    # Глобальный рыночный фильтр: здоровье BTC
    btc_healthy, btc_reason = await check_btc_health(exchange)
    if not btc_healthy:
        logger.warning(f"BTC нездоров ({btc_reason}) — отбор приостановлен")
        return []
    logger.info(f"BTC здоров ({btc_reason})")

    # Тикеры для фильтра по объёму
    try:
        tickers = await exchange.fetch_tickers(all_pairs)
    except Exception as e:
        logger.error(f"Ошибка получения тикеров: {e}")
        return []

    # Стадия 1: фильтр по ликвидности
    liquid_pairs = []
    volumes = {}
    for pair in all_pairs:
        if pair not in tickers:
            continue
        quote_vol = tickers[pair].get('quoteVolume')
        if quote_vol is None:
            continue
        vol = float(quote_vol)
        if vol >= MIN_VOLUME_USDT_24H:
            liquid_pairs.append(pair)
            volumes[pair] = vol

    logger.info(f"После фильтра ликвидности (> ${MIN_VOLUME_USDT_24H:,}): "
                f"{len(liquid_pairs)} пар")

    # Стадия 2: параллельная загрузка часовых свечей
    tasks = [fetch_candles(exchange, pair, '1h', LOOKBACK_1H) for pair in liquid_pairs]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # BTC returns для корреляции
    btc_df = await fetch_candles(exchange, 'BTC/USDT:USDT', '1h', LOOKBACK_1H)
    btc_rets = None
    if btc_df is not None and len(btc_df) > 10:
        btc_rets = np.diff(np.log(btc_df['close'].values))

    # Стадия 3: расчёт trend metrics
    candidates = []  # (pair, hurst, vr720, vol, corr_btc, df_1h)
    for pair, df in zip(liquid_pairs, results):
        if isinstance(df, Exception) or df is None or len(df) < 500:
            continue

        closes = df['close'].values
        rets = np.diff(np.log(closes))

        h = hurst_exponent(rets)
        vr = variance_ratio(rets, 720)
        vol = realized_vol(rets)

        corr_btc = 0.0
        if btc_rets is not None:
            m = min(len(rets), len(btc_rets))
            corr_btc = float(np.corrcoef(rets[-m:], btc_rets[-m:])[0, 1])

        ok_h = h > MIN_HURST
        ok_vr = vr > MIN_VR720
        ok_vol = MIN_VOL < vol < MAX_VOL
        ok_corr = corr_btc < MAX_CORR_BTC

        if ok_h and ok_vr and ok_vol and ok_corr:
            candidates.append((pair, h, vr, vol, corr_btc, df))
            logger.info(f"✓ {pair:<20} H={h:.3f} VR={vr:.3f} "
                        f"Vol={vol * 100:5.1f}% CorrBTC={corr_btc:.3f}")

    logger.info(f"После trend фильтров: {len(candidates)} кандидатов")

    # Стадия 4: корреляционный фильтр между парами
    if len(candidates) < 2:
        return [c[0] for c in candidates]

    # Собираем returns всех кандидатов в DataFrame
    min_len = min(len(c[5]) for c in candidates)
    returns_dict = {}
    for pair, _, _, _, _, df in candidates:
        closes = df['close'].values
        rets = np.diff(np.log(closes))
        returns_dict[pair] = rets[-(min_len - 1):]

    returns_df = pd.DataFrame(returns_dict)
    corr_matrix = returns_df.corr().abs()

    # Score = VR720 (чем больше, тем трендовее пара)
    score_by_pair = {c[0]: c[2] for c in candidates}
    pairs_list = [c[0] for c in candidates]
    rejected = set()

    for i in range(len(pairs_list)):
        for j in range(i + 1, len(pairs_list)):
            a, b = pairs_list[i], pairs_list[j]
            if a in rejected or b in rejected:
                continue
            if corr_matrix.loc[a, b] > MAX_PAIR_CORR:
                # Убираем менее трендовую (меньший VR)
                if score_by_pair[a] < score_by_pair[b]:
                    rejected.add(a)
                    logger.info(f"  ✗ corr({a}, {b}) = {corr_matrix.loc[a, b]:.3f} > "
                                f"{MAX_PAIR_CORR} → удаляем {a}")
                else:
                    rejected.add(b)
                    logger.info(f"  ✗ corr({a}, {b}) = {corr_matrix.loc[a, b]:.3f} > "
                                f"{MAX_PAIR_CORR} → удаляем {b}")

    final_pairs = [p for p in pairs_list if p not in rejected]
    logger.info(f"После корреляционного фильтра: {len(final_pairs)} пар")
    return final_pairs


# ---------- MAIN ----------
async def main():
    start_time = time.time()
    exchange = None

    try:
        exchange = ccxt.bybit({
            'enableRateLimit': True,
            'timeout': 30000,
            'options': {'defaultType': 'swap'},
        })

        pairs = await filter_pairs(exchange)

        elapsed = time.time() - start_time
        logger.info(f"{'=' * 70}")
        logger.info(f"Отбор завершён за {elapsed:.1f} сек.")
        logger.info(f"Отобрано пар: {len(pairs)}")
        logger.info(f"{'=' * 70}")

        # Атомарная запись whitelist
        if pairs:
            atomic_json_write(OUTPUT_FILE, {"pairs": sorted(pairs)})
            logger.info(f"Файл записан: {OUTPUT_FILE}")
            logger.info(f"Пары: {', '.join(sorted(pairs))}")
        else:
            logger.warning("Нет пар для обновления. Предыдущий whitelist сохранён.")

        # Отчёт в Telegram (только количество пар)
        try:
            env_file = os.path.join(USER_DATA_DIR, '.env')
            env_vars = {}
            if os.path.exists(env_file):
                with open(env_file, 'r') as f:
                    for line in f:
                        if '=' in line and not line.startswith('#'):
                            k, v = line.strip().split('=', 1)
                            env_vars[k] = v
            tg_token = env_vars.get('TELEGRAM_TOKEN')
            tg_chat_id = env_vars.get('TELEGRAM_CHAT_ID')

            if tg_token and tg_chat_id:
                msg = f"📊 Momentum universe: {len(pairs)} пар отобрано"
                url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json={"chat_id": tg_chat_id, "text": msg},
                                            timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            logger.info("Отчёт отправлен в Telegram.")
                        else:
                            logger.warning(f"Telegram вернул статус {resp.status}")
            else:
                logger.warning("Telegram-токены не найдены — отправка пропущена.")
        except Exception as e:
            logger.error(f"Ошибка отправки в Telegram: {type(e).__name__}")

    except Exception as e:
        logger.error(f"Критическая ошибка в main: {type(e).__name__}: {e}")
        logger.error(f"Traceback:\n{traceback.format_exc()}")
    finally:
        if exchange is not None:
            try:
                await exchange.close()
                logger.info("Сессия Bybit закрыта.")
            except Exception as e:
                logger.error(f"Ошибка закрытия сессии: {type(e).__name__}: {e}")


if __name__ == '__main__':
    asyncio.run(main())