from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import duckdb

from .config import DEFAULT_DB_PATH, DEFAULT_REPORT_ROOT
from .parquet_reader import inspect_parquet


def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def connect(db_path=DEFAULT_DB_PATH, memory_limit=None, threads=None):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(db_path))
    if memory_limit:
        safe = str(memory_limit).replace("'", "''")
        connection.execute(f"SET memory_limit = '{safe}'")
    if threads is not None:
        if int(threads) < 1:
            raise ValueError("threads must be at least one")
        connection.execute(f"SET threads = {int(threads)}")
    connection.execute("SET TimeZone = 'UTC'")
    migrate(connection)
    return connection


def migrate(connection):
    schema_path = Path(__file__).with_name("schema.sql")
    connection.execute(schema_path.read_text(encoding="utf-8"))
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at_ms BIGINT NOT NULL)"
    )
    run_columns = {
        row[1] for row in connection.execute("PRAGMA table_info('data_quality_runs')").fetchall()
    }
    if "peak_memory_bytes" not in run_columns:
        connection.execute("ALTER TABLE data_quality_runs ADD COLUMN peak_memory_bytes BIGINT")
    connection.execute(
        "INSERT INTO schema_migrations VALUES (2, ?) ON CONFLICT DO NOTHING",
        [now_ms()],
    )
    connection.execute(
        "INSERT INTO schema_migrations VALUES (1, ?) ON CONFLICT DO NOTHING",
        [now_ms()],
    )


def sha256_file(path, chunk_size=4 * 1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def stable_dataset_id(path):
    return hashlib.sha256(str(Path(path).resolve()).encode("utf-8")).hexdigest()


def register_dataset(connection, path, symbol, timeframe):
    path = Path(path).resolve()
    info = inspect_parquet(path)
    source_hash = sha256_file(path)
    stat = path.stat()
    schema_json = json.dumps(info["schema"], ensure_ascii=False)
    dataset_id = stable_dataset_id(path)
    timestamp = now_ms()
    connection.execute(
        """
        INSERT INTO datasets (
            dataset_id, symbol, timeframe, source_path, source_sha256,
            source_size_bytes, source_mtime_ns, source_rows, schema_json,
            discovered_at_ms, validation_status, last_validation_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL)
        ON CONFLICT (dataset_id) DO UPDATE SET
            source_sha256 = excluded.source_sha256,
            source_size_bytes = excluded.source_size_bytes,
            source_mtime_ns = excluded.source_mtime_ns,
            source_rows = excluded.source_rows,
            schema_json = excluded.schema_json,
            discovered_at_ms = excluded.discovered_at_ms,
            validation_status = CASE
                WHEN datasets.source_sha256 != excluded.source_sha256 THEN 'PENDING'
                ELSE datasets.validation_status END,
            last_validation_run_id = CASE
                WHEN datasets.source_sha256 != excluded.source_sha256 THEN NULL
                ELSE datasets.last_validation_run_id END
        """,
        [dataset_id, symbol, timeframe, str(path), source_hash, stat.st_size,
         stat.st_mtime_ns, info["rows"], schema_json, timestamp],
    )
    connection.execute(
        """
        INSERT INTO symbols (symbol, updated_at_ms)
        VALUES (?, ?)
        ON CONFLICT (symbol) DO UPDATE SET updated_at_ms = excluded.updated_at_ms
        """,
        [symbol, timestamp],
    )
    return dataset_id


def dataset_paths(connection, timeframe=None):
    if timeframe:
        rows = connection.execute(
            "SELECT source_path, symbol, timeframe, dataset_id, source_sha256, source_rows "
            "FROM datasets WHERE timeframe = ? ORDER BY symbol, timeframe, source_path",
            [timeframe],
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT source_path, symbol, timeframe, dataset_id, source_sha256, source_rows "
            "FROM datasets ORDER BY symbol, timeframe, source_path"
        ).fetchall()
    return rows


def update_view(connection):
    from .parquet_reader import create_market_view

    paths = [(path, symbol, timeframe)
             for path, symbol, timeframe, *_ in dataset_paths(connection)]
    create_market_view(connection, paths)
    connection.execute(
        """
        CREATE OR REPLACE VIEW valid_market_candles AS
        SELECT candle.symbol, candle.timeframe, candle.source_path, candle.source_row_number,
               candle.open_time_ms, candle.open, candle.high, candle.low,
               candle.close, candle.volume, candle.close_time_ms, candle.quote_volume, candle.count,
               candle.taker_buy_volume, candle.taker_buy_quote_volume, candle.ignore
        FROM market_candles AS candle
        JOIN datasets AS dataset USING (source_path)
        WHERE dataset.validation_status = 'VALIDATED'
          AND NOT EXISTS (
              SELECT 1 FROM data_quality_issues AS issue
              WHERE issue.dataset_id = dataset.dataset_id
                AND issue.run_id = dataset.last_validation_run_id
                AND issue.source_row_number = candle.source_row_number
                AND issue.issue_code NOT IN ('EXACT_DUPLICATE')
          )
        QUALIFY row_number() OVER (
            PARTITION BY candle.symbol, candle.timeframe, candle.open_time_ms
            ORDER BY candle.source_path, candle.source_row_number
        ) = 1
        """
    )


def report_root(data_root):
    return Path(data_root) / DEFAULT_REPORT_ROOT.name
