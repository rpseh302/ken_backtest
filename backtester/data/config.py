from pathlib import Path

TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}

CANONICAL_COLUMNS = (
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
)

COLUMN_ALIASES = {
    "open_time": ("open_time", "Open time"),
    "open": ("open", "Open"),
    "high": ("high", "High"),
    "low": ("low", "Low"),
    "close": ("close", "Close"),
    "volume": ("volume", "Volume"),
    "close_time": ("close_time", "Close time"),
    "quote_volume": ("quote_volume", "Quote asset volume"),
    "count": ("count", "trade_count", "Number of trades"),
    "taker_buy_volume": ("taker_buy_volume", "taker_buy_base_volume", "Taker buy base asset volume"),
    "taker_buy_quote_volume": ("taker_buy_quote_volume", "Taker buy quote asset volume"),
    "ignore": ("ignore", "Ignore"),
}

DEFAULT_DATA_ROOT = Path("data")
DEFAULT_DB_PATH = DEFAULT_DATA_ROOT / "backtester.duckdb"
DEFAULT_REPORT_ROOT = DEFAULT_DATA_ROOT / "reports"
