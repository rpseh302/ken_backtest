from uuid import uuid4

from .registry import now_ms


def start_or_resume_run(connection, data_root, timeframe=None, batch_size=65_536,
                        resume=False, force=False):
    root = str(data_root)
    if resume:
        row = connection.execute(
            """
            SELECT run_id FROM data_quality_runs
            WHERE data_root = ? AND coalesce(timeframe_filter, '') = coalesce(?, '')
              AND status IN ('IN_PROGRESS', 'INTERRUPTED', 'FAILED')
            ORDER BY started_at_ms DESC LIMIT 1
            """,
            [root, timeframe],
        ).fetchone()
        if row:
            connection.execute(
                "UPDATE data_quality_runs SET status='IN_PROGRESS', error_message=NULL WHERE run_id=?",
                [row[0]],
            )
            return row[0], False
    if not force:
        pending = connection.execute(
            "SELECT count(*) FROM datasets WHERE (? IS NULL OR timeframe=?) "
            "AND validation_status NOT IN ('VALIDATED', 'INVALID')",
            [timeframe, timeframe],
        ).fetchone()[0]
        if pending == 0:
            latest = connection.execute(
                """
                SELECT run_id FROM data_quality_runs
                WHERE data_root = ? AND coalesce(timeframe_filter, '') = coalesce(?, '')
                  AND status IN ('COMPLETED', 'COMPLETED_WITH_ERRORS')
                ORDER BY completed_at_ms DESC LIMIT 1
                """,
                [root, timeframe],
            ).fetchone()
            if latest:
                return latest[0], True
    run_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO data_quality_runs
        (run_id, started_at_ms, status, data_root, timeframe_filter, batch_size)
        VALUES (?, ?, 'IN_PROGRESS', ?, ?, ?)
        """,
        [run_id, now_ms(), root, timeframe, batch_size],
    )
    return run_id, False


def record_progress(connection, run_id):
    connection.execute(
        """
        UPDATE data_quality_runs
        SET files_processed = (SELECT count(*) FROM datasets
                               WHERE last_validation_run_id = ? AND validation_status IN ('VALIDATED','INVALID')),
            rows_processed = (SELECT coalesce(sum(source_rows), 0) FROM datasets
                              WHERE last_validation_run_id = ? AND validation_status IN ('VALIDATED','INVALID'))
        WHERE run_id = ?
        """,
        [run_id, run_id, run_id],
    )
