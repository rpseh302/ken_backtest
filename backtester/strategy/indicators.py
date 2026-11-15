import math

def _ema(values, period):
    result = [math.nan] * len(values)
    if len(values) < period:
        return result
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    alpha = 2.0 / (period + 1)
    for index in range(period, len(values)):
        result[index] = values[index] * alpha + result[index - 1] * (1.0 - alpha)
    return result


def _atr(high, low, close, period):
    result = [math.nan] * len(close)
    if not close:
        return result
    true_range = [high[0] - low[0]]
    for index in range(1, len(close)):
        true_range.append(max(
            high[index] - low[index],
            abs(high[index] - close[index - 1]),
            abs(low[index] - close[index - 1]),
        ))
    if len(true_range) < period:
        return result
    result[period - 1] = sum(true_range[:period]) / period
    for index in range(period, len(true_range)):
        result[index] = ((result[index - 1] * (period - 1)) + true_range[index]) / period
    return result


def add_segment_indicators(frame, fast_period, slow_period, atr_period):
    result = frame.copy()
    result["ema_fast"] = math.nan
    result["ema_slow"] = math.nan
    result["atr"] = math.nan
    result["signal"] = None
    for _, indices in result.groupby("segment_index", sort=False).groups.items():
        segment = result.loc[indices]
        close = segment["close"].astype(float).tolist()
        fast = _ema(close, fast_period)
        slow = _ema(close, slow_period)
        atr = _atr(
            segment["high"].astype(float).tolist(),
            segment["low"].astype(float).tolist(),
            close,
            atr_period,
        )
        result.loc[indices, "ema_fast"] = fast
        result.loc[indices, "ema_slow"] = slow
        result.loc[indices, "atr"] = atr
        for offset in range(1, len(indices)):
            previous_fast, previous_slow = fast[offset - 1], slow[offset - 1]
            current_fast, current_slow, current_atr = fast[offset], slow[offset], atr[offset]
            if any(math.isnan(value) for value in (
                previous_fast, previous_slow, current_fast, current_slow, current_atr,
            )):
                continue
            row_index = indices[offset]
            if previous_fast <= previous_slow and current_fast > current_slow:
                result.loc[row_index, "signal"] = "LONG"
            elif previous_fast >= previous_slow and current_fast < current_slow:
                result.loc[row_index, "signal"] = "SHORT"
    return result
