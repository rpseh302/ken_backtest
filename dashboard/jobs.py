from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from datetime import datetime, timezone


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _job_root(data_root):
    path = Path(data_root) / "reports" / "gui_jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def start_backtest_job(data_root, db_path, report_dir, config, symbols):
    root = Path(data_root).resolve()
    job_id = str(uuid.uuid4())
    folder = _job_root(root) / job_id
    folder.mkdir(parents=True, exist_ok=False)
    config_path = folder / "strategy_config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    state_path = folder / "job.json"
    state = {
        "job_id": job_id, "status": "QUEUED", "created_at": _utc_now(),
        "started_at": None, "completed_at": None, "run_id": None,
        "symbols": list(symbols), "config_path": str(config_path),
        "state_path": str(state_path), "log_path": str(folder / "worker.log"),
        "error": None
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [sys.executable, "-m", "dashboard.worker", "--job-file", str(state_path),
               "--data-root", str(root), "--db", str(Path(db_path).resolve()),
               "--report-dir", str(Path(report_dir).resolve())]
    log_handle = open(state["log_path"], "ab")
    kwargs = {"stdout": log_handle, "stderr": subprocess.STDOUT, "cwd": str(Path(__file__).resolve().parents[1])}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **kwargs)
    finally:
        log_handle.close()
    # The worker may update job.json immediately after it starts. Merge the PID
    # into the latest state instead of overwriting a fast completion/failure.
    try:
        latest = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        latest = state
    latest["worker_pid"] = process.pid
    if latest.get("status") == "QUEUED":
        latest["status"] = "RUNNING"
        latest["started_at"] = _utc_now()
    state_path.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    return latest


def list_jobs(data_root, limit=30):
    root = _job_root(data_root)
    jobs = []
    for path in root.glob("*/job.json"):
        try:
            jobs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(jobs, key=lambda item: item.get("created_at", ""), reverse=True)[:limit]


def get_backtest_readiness(db_path):
    """Return validated 1h-universe readiness without modifying the database."""
    import duckdb
    database = Path(db_path)
    if not database.is_file():
        return {"ready": False, "symbols": 0, "segments": 0,
                "reason": "DuckDB 카탈로그가 없습니다. 먼저 데이터 discover 및 validate를 실행하세요."}
    try:
        con = duckdb.connect(str(database), read_only=True)
        try:
            tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
            required = {"symbol_data_coverage", "coverage_segments"}
            if not required.issubset(tables):
                return {"ready": False, "symbols": 0, "segments": 0,
                        "reason": "데이터 검증 테이블이 없습니다. 먼저 python -m backtester data validate를 실행하세요."}
            symbols = con.execute(
                "SELECT count(DISTINCT symbol) FROM symbol_data_coverage "
                "WHERE timeframe='1h' AND eligibility IN ('VALID','PARTIAL')"
            ).fetchone()[0]
            segments = con.execute(
                "SELECT count(*) FROM coverage_segments WHERE timeframe='1h'"
            ).fetchone()[0]
            ready = int(symbols) > 0 and int(segments) > 0
            return {"ready": ready, "symbols": int(symbols), "segments": int(segments),
                    "reason": None if ready else "1h 유효 데이터 세그먼트가 없습니다. 데이터 검증을 완료하세요."}
        finally:
            con.close()
    except Exception as exc:
        return {"ready": False, "symbols": 0, "segments": 0,
                "reason": f"DuckDB readiness 확인 실패: {exc}"}
