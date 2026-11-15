import pyarrow.types as types
import pyarrow.parquet as pq

from .config import CANONICAL_COLUMNS
from .parquet_reader import canonical_column_map, inspect_parquet


NUMERIC_COLUMNS = {
    "open", "high", "low", "close", "volume", "quote_volume", "count",
    "taker_buy_volume", "taker_buy_quote_volume",
}
TIMESTAMP_COLUMNS = {"open_time", "close_time"}


def validate_schema(path):
    info = inspect_parquet(path)
    fields = {field.name: field.type for field in pq.ParquetFile(path).schema_arrow}
    resolved = canonical_column_map(fields)
    issues = []
    for column in set(CANONICAL_COLUMNS) - resolved.keys():
        issues.append(("MISSING_COLUMN", f"Required column {column!r} is absent"))
    for column in NUMERIC_COLUMNS & resolved.keys():
        value_type = fields[resolved[column]]
        if not (types.is_integer(value_type) or types.is_floating(value_type) or types.is_decimal(value_type)):
            issues.append(("INVALID_COLUMN_TYPE", f"{column} must be numeric, got {value_type}"))
    for column in TIMESTAMP_COLUMNS & resolved.keys():
        value_type = fields[resolved[column]]
        if not (types.is_timestamp(value_type) or types.is_integer(value_type)):
            issues.append(("INVALID_COLUMN_TYPE", f"{column} must be timestamp or epoch integer, got {value_type}"))
    return {"rows": info["rows"], "schema": info["schema"], "issues": issues}
