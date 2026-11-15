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
