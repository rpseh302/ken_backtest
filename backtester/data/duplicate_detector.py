def duplicate_summary(connection):
    row = connection.execute(
        """
        WITH grouped AS (
            SELECT open_time_ms, count(*) AS row_count,
                   count(DISTINCT struct_pack(
                       open := open, high := high, low := low, close := close,
                       volume := volume, close_time_ms := close_time_ms,
                       quote_volume := quote_volume, trade_count := count,
                       taker_buy_volume := taker_buy_volume,
                       taker_buy_quote_volume := taker_buy_quote_volume,
                       ignored := ignore
                   )) AS distinct_rows
            FROM current_data WHERE open_time_ms IS NOT NULL
            GROUP BY open_time_ms HAVING count(*) > 1
        )
        SELECT coalesce(sum(row_count), 0),
               coalesce(sum(CASE WHEN distinct_rows = 1 THEN row_count - 1 ELSE 0 END), 0),
               coalesce(sum(CASE WHEN distinct_rows > 1 THEN row_count ELSE 0 END), 0)
        FROM grouped
        """
    ).fetchone()
    return {"duplicate_rows": int(row[0]), "exact_duplicate_extras": int(row[1]),
            "conflicting_duplicate_rows": int(row[2])}
