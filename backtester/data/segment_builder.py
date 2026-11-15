def merge_segments(segments, interval_ms):
    merged = []
    for start_ms, end_ms, candle_count in sorted(segments):
        if merged and start_ms <= merged[-1][1] + interval_ms:
            previous = merged[-1]
            merged[-1] = (previous[0], max(previous[1], end_ms), previous[2] + candle_count)
        else:
            merged.append((start_ms, end_ms, candle_count))
    return merged


def segment_statistics(segments, interval_ms):
    if not segments:
        return {
            "first_open_ms": None, "last_open_ms": None, "expected_candles": 0,
            "observed_candles": 0, "missing_candles": 0, "coverage_ratio": 0.0,
            "segments": [],
        }
    merged = merge_segments(segments, interval_ms)
    first_ms, last_ms = merged[0][0], merged[-1][1]
    expected = (last_ms - first_ms) // interval_ms + 1
    observed = sum(segment[2] for segment in merged)
    return {
        "first_open_ms": first_ms, "last_open_ms": last_ms,
        "expected_candles": expected, "observed_candles": observed,
        "missing_candles": max(0, expected - observed),
        "coverage_ratio": observed / expected if expected else 0.0,
        "segments": merged,
    }
