from collections import defaultdict
import logging
import math

from .candle_validator import interval_ms, valid_row_predicate, validation_conditions
from .config import TIMEFRAME_MS
from .gap_detector import detect_gaps
from .parquet_reader import create_normalized_temp_view
from .quality_reporter import process_peak_memory_bytes
from .recovery_manager import record_progress, start_or_resume_run
from .registry import dataset_paths, now_ms
from .schema_validator import validate_schema
from .segment_builder import segment_statistics


LOGGER = logging.getLogger("backtester.validation")


def _insert_file_issue(connection, run_id, dataset, code, details):
    path, symbol, timeframe, dataset_id, *_ = dataset
    connection.execute(
        """
        INSERT INTO data_quality_issues
        SELECT md5(? || ':' || ? || ':file:' || ?), ?, ?, ?, ?, ?, NULL, NULL, ?, ?
        ON CONFLICT (issue_id) DO NOTHING
        """,
        [run_id, dataset_id, code, run_id, dataset_id, symbol, timeframe, path, code, details],
    )


def _create_annotated_view(connection):
    connection.execute(
        """
        CREATE OR REPLACE TEMP VIEW current_annotated AS
        SELECT *, lag(open_time_ms) OVER (ORDER BY source_row_number) AS previous_open_time_ms,
               count(*) OVER (PARTITION BY open_time_ms) AS duplicate_count,
               count(DISTINCT struct_pack(
                   open := open, high := high, low := low, close := close,
                   volume := volume, close_time_ms := close_time_ms,
                   quote_volume := quote_volume, trade_count := count,
                   taker_buy_volume := taker_buy_volume,
                   taker_buy_quote_volume := taker_buy_quote_volume,
                   ignored := ignore
               )) OVER (PARTITION BY open_time_ms) AS duplicate_variants
        FROM current_data
        """
    )


def _record_row_issues(connection, run_id, dataset, conditions):
    path, symbol, timeframe, dataset_id, *_ = dataset
    selects = []
    for code, condition in conditions.items():
        selects.append(
            f"SELECT source_row_number, open_time_ms, '{code}' AS issue_code, "
            f"'{code.replace('_', ' ').lower()}' AS details "
            f"FROM current_annotated WHERE COALESCE(({condition}), FALSE)"
        )
    exact_duplicate = (
        "duplicate_count > 1 AND duplicate_variants = 1"
    )
    selects.append(
        "SELECT source_row_number, open_time_ms, 'EXACT_DUPLICATE' AS issue_code, "
        "'Exact duplicate candle; retained once in the query view' AS details "
        f"FROM current_annotated WHERE {exact_duplicate}"
    )
    connection.execute(
        f"""
        INSERT INTO data_quality_issues
        SELECT md5('{run_id}:{dataset_id}:' || CAST(source_row_number AS VARCHAR) || ':' || issue_code),
               '{run_id}', '{dataset_id}', '{symbol.replace("'", "''")}',
               '{timeframe}', '{path.replace("'", "''")}', source_row_number,
               open_time_ms, issue_code, details
        FROM ({' UNION ALL '.join(selects)}) AS issues
        ON CONFLICT (issue_id) DO NOTHING
        """
    )


def _segments_for_file(connection, dataset, run_started_ms, abs_tolerance, rel_tolerance):
    timeframe = dataset[2]
    conditions = validation_conditions(
        interval_ms(timeframe), run_started_ms, abs_tolerance, rel_tolerance
    )
    predicate = valid_row_predicate(conditions)
    return detect_gaps(connection, predicate, interval_ms(timeframe))


def _aggregate_coverage(connection, datasets, run_started_ms,
                        abs_tolerance, rel_tolerance, batch_size,
                        timeframe_filter=None):
    grouped = defaultdict(lambda: {"segments": [], "invalid": 0, "incomplete": 0, "duplicates": 0})
    for dataset in datasets:
        path, symbol, timeframe, dataset_id, *_ = dataset
        if timeframe_filter and timeframe != timeframe_filter:
            continue
        status = connection.execute(
            "SELECT validation_status FROM datasets WHERE dataset_id=?", [dataset_id]
        ).fetchone()[0]
        segments = []
        if status == "VALIDATED":
            connection.execute("BEGIN TRANSACTION")
            try:
                create_normalized_temp_view(connection, path, batch_size=batch_size)
                _create_annotated_view(connection)
                segments = _segments_for_file(
                    connection, dataset, run_started_ms, abs_tolerance, rel_tolerance,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        row = connection.execute(
            """
            SELECT
                count(DISTINCT CASE WHEN issue_code NOT IN ('EXACT_DUPLICATE','INCOMPLETE_CANDLE')
                    THEN coalesce(CAST(open_time_ms AS VARCHAR),
                                  'row:' || CAST(source_row_number AS VARCHAR)) END),
                count(DISTINCT CASE WHEN issue_code='INCOMPLETE_CANDLE' THEN open_time_ms END),
                count(DISTINCT CASE WHEN issue_code IN ('EXACT_DUPLICATE','CONFLICTING_DUPLICATE')
                                    THEN open_time_ms END),
                count(*) FILTER (WHERE issue_code='EXACT_DUPLICATE')
                  - count(DISTINCT open_time_ms) FILTER (WHERE issue_code='EXACT_DUPLICATE')
            FROM data_quality_issues WHERE run_id=(
                SELECT last_validation_run_id FROM datasets WHERE dataset_id=?
            ) AND dataset_id=?
            """,
            [dataset_id, dataset_id],
        ).fetchone()
        target = grouped[(symbol, timeframe)]
        target["segments"].extend(segments)
        target["invalid"] += int(row[0] or 0)
        target["incomplete"] += int(row[1] or 0)
        target["duplicates"] += int(row[2] or 0) + int(row[3] or 0)

    symbols = [row[0] for row in connection.execute("SELECT symbol FROM symbols").fetchall()]
    timeframes = [timeframe_filter] if timeframe_filter else list(TIMEFRAME_MS)
    timestamp = now_ms()
    for symbol in symbols:
        for timeframe in timeframes:
            info = grouped.get((symbol, timeframe))
            if not info:
                connection.execute(
                    """
                    INSERT INTO symbol_data_coverage
                    (symbol,timeframe,expected_candles,observed_candles,missing_candles,
                     duplicate_candles,invalid_candles,incomplete_candles,coverage_ratio,
                     eligibility,updated_at_ms)
                    VALUES (?, ?, 0, 0, 0, 0, 0, 0, 0, 'MISSING', ?)
                    ON CONFLICT (symbol,timeframe) DO UPDATE SET
                    first_open_ms=NULL,last_open_ms=NULL,expected_candles=0,observed_candles=0,
                    missing_candles=0,duplicate_candles=0,invalid_candles=0,
                    incomplete_candles=0,coverage_ratio=0,eligibility='MISSING',
                    updated_at_ms=excluded.updated_at_ms
                    """,
                    [symbol, timeframe, timestamp],
                )
                connection.execute("DELETE FROM coverage_segments WHERE symbol=? AND timeframe=?",
                                   [symbol, timeframe])
                continue
            stats = segment_statistics(info["segments"], interval_ms(timeframe))
            if not stats["observed_candles"]:
                eligibility = "INVALID"
            elif info["invalid"]:
                eligibility = "INVALID"
            elif stats["missing_candles"] or info["incomplete"]:
                eligibility = "PARTIAL"
            else:
                eligibility = "VALID"
            connection.execute(
                """
                INSERT INTO symbol_data_coverage
                (symbol,timeframe,first_open_ms,last_open_ms,expected_candles,observed_candles,
                 missing_candles,duplicate_candles,invalid_candles,incomplete_candles,
                 coverage_ratio,eligibility,updated_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (symbol,timeframe) DO UPDATE SET
                first_open_ms=excluded.first_open_ms,last_open_ms=excluded.last_open_ms,
                expected_candles=excluded.expected_candles,observed_candles=excluded.observed_candles,
                missing_candles=excluded.missing_candles,duplicate_candles=excluded.duplicate_candles,
                invalid_candles=excluded.invalid_candles,incomplete_candles=excluded.incomplete_candles,
                coverage_ratio=excluded.coverage_ratio,eligibility=excluded.eligibility,
                updated_at_ms=excluded.updated_at_ms
                """,
                [symbol, timeframe, stats["first_open_ms"], stats["last_open_ms"],
                 stats["expected_candles"], stats["observed_candles"], stats["missing_candles"],
                 info["duplicates"], info["invalid"], info["incomplete"],
                 stats["coverage_ratio"], eligibility, timestamp],
            )
            connection.execute("DELETE FROM coverage_segments WHERE symbol=? AND timeframe=?",
                               [symbol, timeframe])
            for index, (start_ms, end_ms, candle_count) in enumerate(stats["segments"], 1):
                connection.execute(
                    "INSERT INTO coverage_segments VALUES (?, ?, ?, ?, ?, ?)",
                    [symbol, timeframe, index, start_ms, end_ms, candle_count],
                )


def validate(connection, data_root, timeframe=None, batch_size=65_536,
             abs_tolerance=1e-12, rel_tolerance=1e-9, resume=False,
             force=False):
    if timeframe and timeframe not in TIMEFRAME_MS:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    if (not math.isfinite(abs_tolerance) or abs_tolerance < 0
            or not math.isfinite(rel_tolerance) or rel_tolerance < 0):
        raise ValueError("tolerances must be finite and non-negative")
    if batch_size < 1:
        raise ValueError("batch_size must be at least one")
    datasets = dataset_paths(connection, timeframe)
    if not datasets:
        raise FileNotFoundError("No discovered datasets match the validation request")
    run_id, already_complete = start_or_resume_run(
        connection, data_root, timeframe, batch_size, resume, force
    )
    if already_complete:
        LOGGER.info("No pending datasets; reusing completed run %s", run_id)
        return run_id
    run = connection.execute(
        "SELECT started_at_ms FROM data_quality_runs WHERE run_id=?", [run_id]
    ).fetchone()
    run_started_ms = int(run[0])
    total = len(datasets)
    connection.execute("UPDATE data_quality_runs SET files_total=? WHERE run_id=?", [total, run_id])
    failures = []
    for index, dataset in enumerate(datasets, 1):
        path, symbol, current_timeframe, dataset_id, source_hash = dataset[:5]
        status = connection.execute(
            "SELECT validation_status,last_validation_run_id,source_sha256 FROM datasets WHERE dataset_id=?",
            [dataset_id],
        ).fetchone()
        if (status[0] in ("VALIDATED", "INVALID") and status[1] == run_id and status[2] == source_hash):
            continue
        LOGGER.info("Validating %s %s (%d/%d)", symbol, current_timeframe, index, total)
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                "DELETE FROM data_quality_issues WHERE run_id=? AND dataset_id=? "
                "AND issue_code='PROCESSING_ERROR'",
                [run_id, dataset_id],
            )
            schema = validate_schema(path)
            if schema["issues"]:
                for code, details in schema["issues"]:
                    _insert_file_issue(connection, run_id, dataset, code, details)
                connection.execute(
                    "UPDATE datasets SET validation_status='INVALID',last_validation_run_id=? WHERE dataset_id=?",
                    [run_id, dataset_id],
                )
            else:
                create_normalized_temp_view(connection, path, batch_size=batch_size)
                _create_annotated_view(connection)
                conditions = validation_conditions(
                    interval_ms(current_timeframe), run_started_ms,
                    abs_tolerance, rel_tolerance,
                )
                _record_row_issues(connection, run_id, dataset, conditions)
                connection.execute(
                    "UPDATE datasets SET validation_status='VALIDATED',last_validation_run_id=? WHERE dataset_id=?",
                    [run_id, dataset_id],
                )
            connection.execute("COMMIT")
        except Exception as exc:
            connection.execute("ROLLBACK")
            failures.append({"path": path, "error": str(exc)})
            LOGGER.exception("Validation failed for %s", path)
            connection.execute("BEGIN TRANSACTION")
            _insert_file_issue(connection, run_id, dataset, "PROCESSING_ERROR", str(exc))
            connection.execute(
                "UPDATE datasets SET validation_status='ERROR',last_validation_run_id=? WHERE dataset_id=?",
                [run_id, dataset_id],
            )
            connection.execute("COMMIT")
        record_progress(connection, run_id)
        LOGGER.info("Progress %d/%d", index, total)

    completed_datasets = [
        row for row in datasets
        if connection.execute(
            "SELECT validation_status FROM datasets WHERE dataset_id=?", [row[3]]
        ).fetchone()[0] in ("VALIDATED", "INVALID", "ERROR")
    ]
    _aggregate_coverage(
        connection, completed_datasets, run_started_ms,
        abs_tolerance, rel_tolerance, batch_size, timeframe,
    )
    peak_memory_bytes = process_peak_memory_bytes()
    end_status = "COMPLETED_WITH_ERRORS" if failures else "COMPLETED"
    connection.execute(
        "UPDATE data_quality_runs SET status=?,completed_at_ms=?,peak_memory_bytes=?,error_message=? "
        "WHERE run_id=?",
        [end_status, now_ms(), peak_memory_bytes,
         "\n".join(f["error"] for f in failures) or None, run_id],
    )
    record_progress(connection, run_id)
    return run_id
