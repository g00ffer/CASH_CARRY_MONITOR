import pandas as pd
import numpy as np
from sklearn.cluster import DBSCAN
from typing import Tuple, List, Dict

class GerchikLevelDetector:
    """
    Алгоритм поиска сильных ценовых уровней в стиле А. Герчика.
    """
    def __init__(self, 
                 left_bars: int = 5, 
                 right_bars: int = 5,
                 atr_period: int = 14,
                 cluster_atr_mult: float = 1.5,
                 min_touches: int = 2,
                 score_threshold: float = 40.0):
        """
        :param left_bars: Баров слева для подтверждения фрактала
        :param right_bars: Баров справа для подтверждения фрактала
        :param atr_period: Период ATR для динамического радиуса кластера
        :param cluster_atr_mult: Множитель ATR для определения ширины зоны (eps в DBSCAN)
        :param min_touches: Минимальное количество касаний для уровня
        :param score_threshold: Порог отсечения слабых уровней (0-100)
        """
        self.left_bars = left_bars
        self.right_bars = right_bars
        self.atr_period = atr_period
        self.cluster_atr_mult = cluster_atr_mult
        self.min_touches = min_touches
        self.score_threshold = score_threshold

    def _calculate_atr(self, df: pd.DataFrame) -> pd.Series:
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        return true_range.rolling(window=self.atr_period).mean()

    def _find_pivots(self, df: pd.DataFrame) -> pd.DataFrame:
        """Поиск значимых Swing High и Swing Low (фракталы)."""
        pivots = []
        # Простая векторизованная проверка фракталов
        for i in range(self.left_bars, len(df) - self.right_bars):
            # Swing High
            is_high = True
            for j in range(1, self.left_bars + 1):
                if df['high'].iloc[i-j] >= df['high'].iloc[i]:
                    is_high = False
                    break
            if is_high:
                for j in range(1, self.right_bars + 1):
                    if df['high'].iloc[i+j] >= df['high'].iloc[i]:
                        is_high = False
                        break
            if is_high:
                pivots.append({'index': i, 'price': df['high'].iloc[i], 'type': 'high', 'time': df.index[i]})

            # Swing Low
            is_low = True
            for j in range(1, self.left_bars + 1):
                if df['low'].iloc[i-j] <= df['low'].iloc[i]:
                    is_low = False
                    break
            if is_low:
                for j in range(1, self.right_bars + 1):
                    if df['low'].iloc[i+j] <= df['low'].iloc[i]:
                        is_low = False
                        break
            if is_low:
                pivots.append({'index': i, 'price': df['low'].iloc[i], 'type': 'low', 'time': df.index[i]})
                
        return pd.DataFrame(pivots).set_index('index')

    def _cluster_pivots(self, pivots: pd.DataFrame, atr: float) -> pd.DataFrame:
        """Кластеризация пивотов в ценовые зоны с помощью DBSCAN."""
        if pivots.empty:
            return pd.DataFrame()
        
        prices = pivots['price'].values.reshape(-1, 1)
        # Динамический eps на основе ATR
        eps = atr * self.cluster_atr_mult 
        
        clustering = DBSCAN(eps=eps, min_samples=2).fit(prices)
        pivots['cluster'] = clustering.labels_
        
        # Фильтруем шум (кластеры с меткой -1)
        return pivots[pivots['cluster'] != -1]

    def _analyze_zone(self, cluster_data: pd.DataFrame, df: pd.DataFrame) -> Dict:
        """Анализ конкретной зоны (кластера) и сбор герчиковских метрик."""
        zone_high = cluster_data['price'].max()
        zone_low = cluster_data['price'].min()
        zone_center = (zone_high + zone_low) / 2
        last_time = cluster_data['time'].max()
        
        metrics = {
            'upper': zone_high,
            'lower': zone_low,
            'center': zone_center,
            'last_time': last_time,
            'touches': 0,
            'false_breakouts': 0,
            'is_mirror': False,
            'reaction_strength': 0.0,
            'round_number_boost': 0,
            'impulse_before': 0.0
        }
        
        # 1. Подсчет касаний и ложных пробоев
        # Ищем бары, где High/Low зашли в зону
        in_zone_mask = ((df['high'] >= zone_low) & (df['low'] <= zone_high))
        zone_indices = df.index[in_zone_mask]
        
        if len(zone_indices) == 0:
            return metrics

        # Группируем последовательные бары в одно "касание"
        touches = 0
        prev_idx = -999
        for idx in zone_indices:
            if idx - prev_idx > 3: # Кулдаун 3 бара, чтобы считать новое касание
                touches += 1
                
                # Проверка ложного пробоя (тень за зону, закрытие внутри)
                bar = df.loc[idx]
                if (bar['high'] > zone_high and bar['close'] < zone_high) or \
                   (bar['low'] < zone_low and bar['close'] > zone_low):
                    metrics['false_breakouts'] += 1
                    
                # Оценка силы реакции (отскока)
                # Смотрим на 5 баров вперед
                future_bars = df.iloc[idx+1 : idx+6]
                if not future_bars.empty:
                    if bar['close'] <= zone_center: # Отскок вверх
                        max_up = future_bars['high'].max()
                        metrics['reaction_strength'] += (max_up - bar['close']) / bar['close'] * 100
                    else: # Отскок вниз
                        min_down = future_bars['low'].min()
                        metrics['reaction_strength'] += (bar['close'] - min_down) / bar['close'] * 100
                        
            prev_idx = idx
            
        metrics['touches'] = touches
        
        # 2. Зеркальность (был high, стал low в этой зоне)
        types_in_zone = cluster_data['type'].unique()
        if len(types_in_zone) > 1:
            metrics['is_mirror'] = True
            
        # 3. Круглые числа (Психология)
        # Определяем порядок цены для поиска круглых чисел
        price_magnitude = len(str(int(zone_center)))
        round_step = 10 ** (price_magnitude - 2) # Например, для 150 -> шаг 10, для 1500 -> шаг 100
        nearest_round = round(zone_center / round_step) * round_step
        distance_pct = abs(zone_center - nearest_round) / zone_center * 100
        if distance_pct < 0.5: # Очень близко к круглому числу
            metrics['round_number_boost'] = 15
        elif distance_pct < 1.5:
            metrics['round_number_boost'] = 5

        # 4. Импульс до уровня (Накопление/Разгон)
        # Проверяем, было ли движение до формирования зоны > 10% (упрощенно)
        first_touch_idx = cluster_data.index.min()
        if first_touch_idx > 20:
            past_price = df['close'].iloc[first_touch_idx - 20]
            move_pct = abs(zone_center - past_price) / past_price * 100
            metrics['impulse_before'] = min(move_pct, 20.0) # Капируем на 20%

        return metrics

    def _calculate_score(self, metrics: Dict) -> float:
        """Система скоринга силы уровня."""
        score = 0.0
        
        # Касания (макс 30 баллов)
        score += min(metrics['touches'] * 6, 30)
        
        # Ложные пробои (макс 25 баллов) - очень важный герчиковский маркер
        score += min(metrics['false_breakouts'] * 12.5, 25)
        
        # Зеркальность (15 баллов)
        if metrics['is_mirror']:
            score += 15
            
        # Сила реакции (макс 15 баллов)
        score += min(metrics['reaction_strength'] * 2, 15)
        
        # Круглое число (макс 15 баллов)
        score += metrics['round_number_boost']
        
        # Импульс до уровня (макс 10 баллов)
        if metrics['impulse_before'] > 10:
            score += 10
        elif metrics['impulse_before'] > 5:
            score += 5
            
        return round(score, 2)

    def detect(self, df: pd.DataFrame) -> pd.DataFrame:
        """Главный пайплайн обнаружения уровней."""
        df = df.copy()
        df.dropna(inplace=True)
        
        # 1. Расчет ATR
        df['atr'] = self._calculate_atr(df)
        current_atr = df['atr'].iloc[-1]
        
        # 2. Поиск пивотов
        pivots = self._find_pivots(df)
        if pivots.empty:
            return pd.DataFrame()
            
        # 3. Кластеризация
        clustered_pivots = self._cluster_pivots(pivots, current_atr)
        if clustered_pivots.empty:
            return pd.DataFrame()
            
        # 4. Анализ зон и скоринг
        levels = []
        for cluster_id in clustered_pivots['cluster'].unique():
            cluster_data = clustered_pivots[clustered_pivots['cluster'] == cluster_id]
            
            # Фильтр по минимальному количеству касаний (пивотов в кластере)
            if len(cluster_data) < self.min_touches:
                continue
                
            metrics = self._analyze_zone(cluster_data, df)
            
            if metrics['touches'] < self.min_touches:
                continue
                
            metrics['score'] = self._calculate_score(metrics)
            metrics['cluster_id'] = cluster_id
            
            if metrics['score'] >= self.score_threshold:
                levels.append(metrics)
                
        if not levels:
            return pd.DataFrame()
            
        levels_df = pd.DataFrame(levels)
        levels_df = levels_df.sort_values(by='score', ascending=False).reset_index(drop=True)
        
        return levels_df