from .config import TIMEFRAME_MS


def validation_conditions(timeframe_ms, now_ms, abs_tolerance=1e-12, rel_tolerance=1e-9):
    tolerance = (
        f"greatest({float(abs_tolerance)!r}, "
        f"abs(CAST(volume AS DOUBLE)) * {float(rel_tolerance)!r})"
    )
    quote_tolerance = (
        f"greatest({float(abs_tolerance)!r}, "
        f"abs(CAST(quote_volume AS DOUBLE)) * {float(rel_tolerance)!r})"
    )
    return {
        "NULL_OR_UNPARSEABLE_REQUIRED_FIELD": "(" + " OR ".join(
            f"{column} IS NULL" for column in (
                "open_time_ms", "open", "high", "low", "close", "volume",
                "close_time_ms", "quote_volume", "count", "taker_buy_volume",
                "taker_buy_quote_volume",
            )
        ) + ")",
        "NONFINITE_VALUE": " OR ".join(
            f"NOT isfinite(CAST({column} AS DOUBLE))"
            for column in (
                "open", "high", "low", "close", "volume", "quote_volume",
                "taker_buy_volume", "taker_buy_quote_volume",
            )
        ),
        "TIMESTAMP_ALIGNMENT": f"open_time_ms % {timeframe_ms} != 0",
        "CLOSE_TIME_MISMATCH": f"close_time_ms != open_time_ms + {timeframe_ms} - 1",
        "OUT_OF_ORDER": "previous_open_time_ms IS NOT NULL AND open_time_ms < previous_open_time_ms",
        "INVALID_OHLC": (
            f"low > least(open, close) + {float(abs_tolerance)!r} OR "
            f"high < greatest(open, close) - {float(abs_tolerance)!r} OR "
            f"low > high + {float(abs_tolerance)!r}"
        ),
        "NEGATIVE_VOLUME": "volume < 0",
        "NEGATIVE_QUOTE_VOLUME": "quote_volume < 0",
        "NEGATIVE_TRADE_COUNT": "count < 0 OR count != floor(count)",
        "TAKER_VOLUME_EXCEEDS_TOTAL": f"taker_buy_volume > volume + {tolerance}",
        "TAKER_QUOTE_VOLUME_EXCEEDS_TOTAL": f"taker_buy_quote_volume > quote_volume + {quote_tolerance}",
        "INCOMPLETE_CANDLE": f"close_time_ms >= {int(now_ms)}",
        "CONFLICTING_DUPLICATE": (
            "open_time_ms IS NOT NULL AND duplicate_count > 1 AND duplicate_variants > 1"
        ),
    }


def valid_row_predicate(conditions):
    excluded = [condition for code, condition in conditions.items()
                if code != "CONFLICTING_DUPLICATE"]
    return " AND ".join(f"NOT COALESCE(({condition}), FALSE)" for condition in excluded) + (
        " AND NOT COALESCE((duplicate_count > 1 AND duplicate_variants > 1), FALSE)"
    )


def interval_ms(timeframe):
    try:
        return TIMEFRAME_MS[timeframe]
    except KeyError as exc:
        raise ValueError(f"Unsupported timeframe: {timeframe}") from exc
