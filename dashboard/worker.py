from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(path, state):
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--job-file",required=True)
    parser.add_argument("--data-root",required=True)
    parser.add_argument("--db",required=True)
    parser.add_argument("--report-dir",required=True)
    args=parser.parse_args()
    state_path=Path(args.job_file)
    state=json.loads(state_path.read_text(encoding="utf-8"))
    state["status"]="RUNNING"
    state["started_at"]=_now()
    _write(state_path,state)
    command=[sys.executable,"-m","backtester","backtest","--strategy","ema_20_50_atr",
             "--config",state["config_path"],"--data-root",args.data_root,
             "--db",args.db,"--report-dir",args.report_dir,"--symbols",*state["symbols"]]
    try:
        result=subprocess.run(command,capture_output=True,text=True,encoding="utf-8",errors="replace",
                               cwd=str(Path(__file__).resolve().parents[1]))
        log_path=Path(state["log_path"])
        log_path.write_text((result.stdout or "") + ("\n--- STDERR ---\n" + result.stderr if result.stderr else ""),
                            encoding="utf-8")
        if result.returncode != 0:
            state["status"]="FAILED"
            state["error"]=(result.stderr or result.stdout or f"Backtest exited with code {result.returncode}")[-6000:]
        else:
            state["status"]="COMPLETED"
            decoder=json.JSONDecoder()
            payload=None
            output=result.stdout or ""
            for index,char in enumerate(output):
                if char=="{":
                    try:
                        candidate,end=decoder.raw_decode(output[index:])
                        if isinstance(candidate,dict) and candidate.get("run_id"):
                            payload=candidate
                    except json.JSONDecodeError:
                        continue
            if payload:
                state["run_id"]=payload.get("run_id")
                state["metrics"]=payload.get("metrics",{})
                state["report_dir"]=payload.get("report_dir")
            else:
                state["error"]="Backtest completed but result JSON could not be parsed; inspect worker.log."
    except Exception as exc:
        state["status"]="FAILED"
        state["error"]=repr(exc)
    state["completed_at"]=_now()
    _write(state_path,state)


if __name__=="__main__":
    main()
