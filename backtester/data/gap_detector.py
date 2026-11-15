def detect_gaps(connection, valid_predicate, interval_ms):
    rows = connection.execute(
        f"""
        WITH eligible AS (
            SELECT open_time_ms
            FROM current_annotated
            WHERE {valid_predicate}
            QUALIFY row_number() OVER (
                PARTITION BY open_time_ms ORDER BY source_row_number
            ) = 1
        ),
        ordered AS (
            SELECT open_time_ms,
                   row_number() OVER (ORDER BY open_time_ms) AS sequence_number
            FROM eligible
        ),
        grouped AS (
            SELECT open_time_ms,
                   open_time_ms - sequence_number * {int(interval_ms)} AS group_key
            FROM ordered
        )
        SELECT min(open_time_ms), max(open_time_ms), count(*)
        FROM grouped GROUP BY group_key ORDER BY min(open_time_ms)
        """
    ).fetchall()
    return [(int(start), int(end), int(count)) for start, end, count in rows]
