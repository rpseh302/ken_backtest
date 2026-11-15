import csv
import json
import os
from pathlib import Path
import tempfile


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=path.name, suffix=".part", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target:
            json.dump(payload, target, ensure_ascii=False, indent=2)
            target.write("\n")
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def discovery_report(connection, discovered, report_dir):
    payload = {
        "dataset_count": len(discovered),
        "symbol_count": connection.execute("SELECT count(*) FROM symbols").fetchone()[0],
        "timeframes": connection.execute(
            "SELECT timeframe, count(*) AS files, count(DISTINCT symbol) AS symbols "
            "FROM datasets GROUP BY timeframe ORDER BY timeframe"
        ).fetchdf().to_dict(orient="records"),
        "datasets": discovered,
    }
    _write_json(Path(report_dir) / "data_discovery.json", payload)
    return payload


def inventory_report(connection, report_dir):
    rows = connection.execute(
        """
        SELECT timeframe, count(*) AS rows, count(DISTINCT symbol) AS symbols,
               min(open_time_ms) AS first_open_ms, max(open_time_ms) AS last_open_ms
        FROM market_candles GROUP BY timeframe ORDER BY timeframe
        """
    ).fetchdf().to_dict(orient="records")
    per_symbol = connection.execute(
        """
        SELECT symbol, timeframe, count(*) AS rows, min(open_time_ms) AS first_open_ms,
               max(open_time_ms) AS last_open_ms
        FROM market_candles GROUP BY symbol, timeframe ORDER BY symbol, timeframe
        """
    ).fetchdf().to_dict(orient="records")
    payload = {"timeframes": rows, "symbol_timeframes": per_symbol}
    _write_json(Path(report_dir) / "data_inventory.json", payload)
    return payload


def quality_report(connection, run_id, report_dir):
    connection.execute(
        """
        UPDATE symbols AS symbol SET
            first_observed_ms = (
                SELECT min(first_open_ms) FROM symbol_data_coverage
                WHERE symbol_data_coverage.symbol = symbol.symbol
            ),
            last_observed_ms = (
                SELECT max(last_open_ms) FROM symbol_data_coverage
                WHERE symbol_data_coverage.symbol = symbol.symbol
            ),
            updated_at_ms = CAST(epoch_ms(current_timestamp) AS BIGINT)
        """
    )
    issue_rows = connection.execute(
        """
        SELECT timeframe, issue_code, count(*) AS records,
               count(DISTINCT symbol) AS symbols
        FROM data_quality_issues WHERE run_id=?
        GROUP BY timeframe, issue_code ORDER BY timeframe, issue_code
        """,
        [run_id],
    ).fetchdf().to_dict(orient="records")
    coverage_rows = connection.execute(
        """
        SELECT timeframe, eligibility, count(*) AS symbols
        FROM symbol_data_coverage GROUP BY timeframe, eligibility
        ORDER BY timeframe, eligibility
        """
    ).fetchdf().to_dict(orient="records")
    segments_by_timeframe = connection.execute(
        """
        SELECT timeframe, count(*) AS contiguous_segments
        FROM coverage_segments GROUP BY timeframe ORDER BY timeframe
        """
    ).fetchdf().to_dict(orient="records")
    per_symbol = connection.execute(
        """
        SELECT coverage.*,
               (SELECT count(*) FROM coverage_segments AS segment
                WHERE segment.symbol=coverage.symbol AND segment.timeframe=coverage.timeframe)
                   AS segment_count
        FROM symbol_data_coverage AS coverage
        ORDER BY timeframe, symbol
        """
    ).fetchdf().to_dict(orient="records")
    run = connection.execute(
        "SELECT * FROM data_quality_runs WHERE run_id=?", [run_id]
    ).fetchdf().to_dict(orient="records")
    payload = {
        "run": run[0] if run else None,
        "issues_by_timeframe": issue_rows,
        "eligibility_by_timeframe": coverage_rows,
        "segments_by_timeframe": segments_by_timeframe,
        "symbol_coverage": per_symbol,
    }
    _write_json(Path(report_dir) / "data_quality.json", payload)
    _write_json(Path(report_dir) / "symbol_coverage.json", {
        "symbol_coverage": per_symbol, "eligibility_by_timeframe": coverage_rows,
    })
    with (Path(report_dir) / "symbol_coverage.csv").open("w", encoding="utf-8-sig", newline="") as target:
        if per_symbol:
            writer = csv.DictWriter(target, fieldnames=list(per_symbol[0]))
            writer.writeheader()
            writer.writerows(per_symbol)
    return payload


def process_peak_memory_bytes():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.restype = wintypes.HANDLE
        get_memory_info = psapi.GetProcessMemoryInfo
        get_memory_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        get_memory_info.restype = wintypes.BOOL
        if get_memory_info(get_current_process(), ctypes.byref(counters), counters.cb):
            return int(counters.PeakWorkingSetSize)
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if os.name == "nt" else value * 1024)
    except (ImportError, AttributeError):
        return None


def performance_report(connection, report_dir):
    rows = connection.execute(
        """
        SELECT run_id, status, files_total, files_processed, rows_processed,
               started_at_ms, completed_at_ms, peak_memory_bytes,
               CASE WHEN completed_at_ms IS NULL THEN NULL
                    ELSE (completed_at_ms - started_at_ms) / 1000.0 END AS elapsed_seconds
        FROM data_quality_runs ORDER BY started_at_ms DESC
        """
    ).fetchdf().to_dict(orient="records")
    payload = {"runs": rows, "report_process_peak_memory_bytes": process_peak_memory_bytes()}
    _write_json(Path(report_dir) / "performance.json", payload)
    return payload
