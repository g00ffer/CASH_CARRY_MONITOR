def analyze_zone(cluster, highs, lows, closes):
    prices = [p['price'] for p in cluster]
    zone_high = max(prices)
    zone_low = min(prices)
    zone_center = (zone_high + zone_low) / 2

    metrics = {
        'upper': zone_high, 'lower': zone_low, 'center': zone_center,
        'touches': 0, 'false_breakouts': 0, 'is_mirror': False,
        'reaction_strength': 0.0, 'round_number_boost': 0, 'impulse_before': 0.0,
    }

    if len(set(p['type'] for p in cluster)) > 1:
        metrics['is_mirror'] = True

    n = len(closes)
    touches = 0
    prev_touch_idx = -999
    for i in range(n):
        if highs[i] >= zone_low and lows[i] <= zone_high:
            # FIX: ложный пробой фиксируется НЕЗАВИСИМО от кулдауна касаний
            if (highs[i] > zone_high and closes[i] < zone_high) or \
               (lows[i] < zone_low and closes[i] > zone_low):
                metrics['false_breakouts'] += 1

            # Кулдаун 3 бара применяется ТОЛЬКО к счётчику касаний
            if i - prev_touch_idx > 3:
                touches += 1
                prev_touch_idx = i

                # Сила реакции (отскока) — остаётся привязанной к касанию
                if i + 5 < n:
                    if closes[i] <= zone_center:  # отскок вверх
                        max_up = np.max(highs[i + 1:i + 6])
                        metrics['reaction_strength'] += (max_up - closes[i]) / closes[i] * 100
                    else:  # отскок вниз
                        min_down = np.min(lows[i + 1:i + 6])
                        metrics['reaction_strength'] += (closes[i] - min_down) / closes[i] * 100
    metrics['touches'] = touches

    if zone_center > 0:
        mag = len(str(int(zone_center)))
        if mag >= 2:
            step = 10 ** (mag - 2)
            nearest = round(zone_center / step) * step
            dist = abs(zone_center - nearest) / zone_center * 100
            if dist < 0.5:
                metrics['round_number_boost'] = 15
            elif dist < 1.5:
                metrics['round_number_boost'] = 5

    first_idx = min(p['index'] for p in cluster)
    if first_idx > 20:
        past = closes[first_idx - 20]
        metrics['impulse_before'] = min(abs(zone_center - past) / past * 100, 20.0)

    return metrics