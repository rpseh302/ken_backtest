from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .config import CANONICAL_COLUMNS, COLUMN_ALIASES


class ParquetSchemaError(ValueError):
    pass


def inspect_parquet(path):
    parquet = pq.ParquetFile(path)
    return {
        "rows": parquet.metadata.num_rows,
        "schema": [(field.name, str(field.type), field.nullable) for field in parquet.schema_arrow],
    }


def canonical_column_map(column_names):
    available = set(column_names)
    resolved = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        matches = [name for name in aliases if name in available]
        if matches:
            resolved[canonical] = matches[0]
    return resolved


def validate_required_columns(column_names):
    resolved = canonical_column_map(column_names)
    missing = sorted(set(CANONICAL_COLUMNS) - resolved.keys())
    if missing:
        raise ParquetSchemaError(f"Missing required columns: {', '.join(missing)}")
    return resolved


def _quoted_identifier(value):
    return '"' + value.replace('"', '""') + '"'


def _timestamp_ms(expression, arrow_type):
    type_text = str(arrow_type).lower()
    if "timestamp" in type_text:
        if getattr(arrow_type, "tz", None):
            return f"epoch_ms({expression})"
        return f"epoch_ms({expression} AT TIME ZONE 'UTC')"
    if type_text in ("date32", "date64"):
        return f"epoch_ms(CAST({expression} AS TIMESTAMP) AT TIME ZONE 'UTC')"
    return (
        f"CASE WHEN abs(CAST({expression} AS DOUBLE)) >= 1e17 "
        f"THEN CAST(CAST({expression} AS BIGINT) / 1000000 AS BIGINT) "
        f"WHEN abs(CAST({expression} AS DOUBLE)) >= 1e14 "
        f"THEN CAST(CAST({expression} AS BIGINT) / 1000 AS BIGINT) "
        f"ELSE CAST({expression} AS BIGINT) END"
    )


def normalized_projection(path, alias="src"):
    parquet = pq.ParquetFile(path)
    fields = {field.name: field.type for field in parquet.schema_arrow}
    resolved = validate_required_columns(fields)
    projections = ["file_row_number AS source_row_number"]
    for canonical in CANONICAL_COLUMNS:
        source = resolved[canonical]
        expression = f"{alias}.{_quoted_identifier(source)}"
        if canonical in ("open_time", "close_time"):
            expression = _timestamp_ms(expression, fields[source])
            output = f"{canonical}_ms"
        else:
            output = canonical
            if canonical != "ignore":
                expression = f"TRY_CAST({expression} AS DOUBLE)"
        projections.append(f"{expression} AS {_quoted_identifier(output)}")
    return ", ".join(projections)


def create_normalized_temp_view(connection, path, view_name="current_data", batch_size=65_536):
    if batch_size < 1:
        raise ValueError("batch_size must be at least one")
    projection = normalized_projection(path)
    connection.execute("DROP VIEW IF EXISTS current_annotated")
    connection.execute(f"DROP TABLE IF EXISTS {_quoted_identifier(view_name)}")
    parquet = pq.ParquetFile(path)
    row_offset = 0
    for batch in parquet.iter_batches(batch_size=batch_size):
        source_row_number = pa.array(
            range(row_offset, row_offset + batch.num_rows), type=pa.int64()
        )
        batch = pa.record_batch(
            [batch.column(index) for index in range(batch.num_columns)] + [source_row_number],
            names=list(batch.schema.names) + ["file_row_number"],
        )
        connection.register("_parquet_batch", batch)
        if row_offset == 0:
            connection.execute(
                f"CREATE TEMP TABLE {_quoted_identifier(view_name)} AS "
                f"SELECT {projection} FROM _parquet_batch AS src"
            )
        else:
            connection.execute(
                f"INSERT INTO {_quoted_identifier(view_name)} "
                f"SELECT {projection} FROM _parquet_batch AS src"
            )
        connection.unregister("_parquet_batch")
        row_offset += batch.num_rows
    if row_offset == 0:
        raise ValueError(f"Parquet file has no records: {path}")


def create_market_view(connection, paths, view_name="market_candles"):
    connection.execute("DROP VIEW IF EXISTS valid_market_candles")
    connection.execute(f"DROP VIEW IF EXISTS {_quoted_identifier(view_name)}")
    if not paths:
        connection.execute(
            f"CREATE OR REPLACE VIEW {_quoted_identifier(view_name)} AS "
            "SELECT CAST(NULL AS VARCHAR) AS symbol, CAST(NULL AS VARCHAR) AS timeframe, "
            "CAST(NULL AS BIGINT) AS open_time_ms, CAST(NULL AS DOUBLE) AS open, "
            "CAST(NULL AS DOUBLE) AS high, CAST(NULL AS DOUBLE) AS low, "
            "CAST(NULL AS DOUBLE) AS close, CAST(NULL AS DOUBLE) AS volume, "
            "CAST(NULL AS BIGINT) AS close_time_ms, CAST(NULL AS DOUBLE) AS quote_volume, "
            "CAST(NULL AS BIGINT) AS count, CAST(NULL AS DOUBLE) AS taker_buy_volume, "
            "CAST(NULL AS DOUBLE) AS taker_buy_quote_volume, CAST(NULL AS VARCHAR) AS ignore "
            "WHERE FALSE"
        )
        return
    selects = []
    for path, symbol, timeframe in paths:
        projection = normalized_projection(path)
        sql_path = str(Path(path).resolve()).replace("'", "''")
        symbol_sql = "'" + symbol.replace("'", "''") + "'"
        timeframe_sql = "'" + timeframe.replace("'", "''") + "'"
        selects.append(
            f"SELECT {symbol_sql} AS symbol, {timeframe_sql} AS timeframe, "
            f"'{sql_path}' AS source_path, {projection} "
            f"FROM read_parquet('{sql_path}', file_row_number=true) AS src"
        )
    connection.execute(
        f"CREATE OR REPLACE VIEW {_quoted_identifier(view_name)} AS "
        + " UNION ALL ".join(selects)
    )
