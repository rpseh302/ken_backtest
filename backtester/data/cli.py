import argparse
import json
import logging
from pathlib import Path
import sys

from .config import DEFAULT_DATA_ROOT, DEFAULT_DB_PATH, DEFAULT_REPORT_ROOT, TIMEFRAME_MS
from .data_discovery import discover
from .quality_reporter import (
    discovery_report, inventory_report, performance_report, quality_report,
)
from .registry import connect
from .validation_runner import validate
from ..strategy.config import load_config
from ..strategy.runner import execute_backtest


LOGGER = logging.getLogger("backtester")


def _parser():
    parser = argparse.ArgumentParser(prog="python -m backtester")
    groups = parser.add_subparsers(dest="group", required=True)
    data = groups.add_parser("data", help="discover and validate market data")
    commands = data.add_subparsers(dest="command", required=True)
    for name in ("discover", "inventory", "validate", "report", "status", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
        command.add_argument("--db", type=Path)
        command.add_argument("--report-dir", type=Path)
        command.add_argument("--memory-limit", default="4GB")
        command.add_argument("--threads", type=int, default=4)
        if name in ("validate", "resume"):
            command.add_argument("--timeframe", choices=tuple(TIMEFRAME_MS))
            command.add_argument("--batch-size", type=int, default=65_536)
            command.add_argument("--abs-tolerance", type=float, default=1e-12)
            command.add_argument("--rel-tolerance", type=float, default=1e-9)
            command.add_argument("--force", action="store_true")
    backtest = groups.add_parser("backtest", help="run a configured strategy backtest")
    backtest.add_argument("--strategy", required=True, choices=("ema_20_50_atr",))
    backtest.add_argument("--config", type=Path, default=Path("config/ema_20_50_atr.json"))
    backtest.add_argument("--symbols", nargs="+")
    backtest.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    backtest.add_argument("--db", type=Path)
    backtest.add_argument("--report-dir", type=Path)
    backtest.add_argument("--memory-limit", default="4GB")
    backtest.add_argument("--threads", type=int, default=4)
    return parser


def _json_print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _latest_run(connection, root):
    row = connection.execute(
        "SELECT run_id FROM data_quality_runs WHERE data_root=? "
        "ORDER BY (timeframe_filter IS NOT NULL), started_at_ms DESC LIMIT 1", [str(root)]
    ).fetchone()
    return row[0] if row else None


def _status(connection):
    runs = connection.execute(
        """
        SELECT run_id,status,started_at_ms,completed_at_ms,files_total,files_processed,
               rows_processed,error_message FROM data_quality_runs
        ORDER BY started_at_ms DESC LIMIT 5
        """
    ).fetchdf().to_dict(orient="records")
    datasets = connection.execute(
        "SELECT validation_status,count(*) AS files FROM datasets GROUP BY validation_status ORDER BY validation_status"
    ).fetchdf().to_dict(orient="records")
    eligibility = connection.execute(
        """
        SELECT timeframe,eligibility,count(*) AS symbols
        FROM symbol_data_coverage GROUP BY timeframe,eligibility
        ORDER BY timeframe,eligibility
        """
    ).fetchdf().to_dict(orient="records")
    return {"runs": runs, "datasets": datasets, "eligibility": eligibility}


def main(argv=None):
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="backslashreplace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parser().parse_args(argv)
    root = args.data_root.resolve()
    db_path = args.db or root / DEFAULT_DB_PATH.name
    report_dir = args.report_dir or root / DEFAULT_REPORT_ROOT.name
    connection = connect(db_path, args.memory_limit, args.threads)
    try:
        if args.group == "backtest":
            config = load_config(args.config)
            if config.name != args.strategy:
                raise ValueError(f"Configuration strategy {config.name!r} does not match --strategy")
            _json_print(execute_backtest(connection, root, report_dir, config, args.symbols))
            return 0
        if args.command == "status":
            _json_print(_status(connection))
            return 0
        if args.command == "report":
            run_id = _latest_run(connection, root)
            if not run_id:
                raise RuntimeError("No quality run exists; run data validate first")
            inventory_report(connection, report_dir)
            quality_report(connection, run_id, report_dir)
            performance_report(connection, report_dir)
            _json_print({"run_id": run_id, "report_dir": str(report_dir)})
            return 0
        discovered = discover(connection, root)
        if args.command == "discover":
            payload = discovery_report(connection, discovered, report_dir)
            _json_print({"dataset_count": payload["dataset_count"],
                         "symbol_count": payload["symbol_count"],
                         "report": str(report_dir / "data_discovery.json")})
            return 0
        if args.command == "inventory":
            discovery_report(connection, discovered, report_dir)
            payload = inventory_report(connection, report_dir)
            _json_print({"timeframes": payload["timeframes"],
                         "report": str(report_dir / "data_inventory.json")})
            return 0
        if args.command in ("validate", "resume"):
            if args.batch_size < 1:
                raise ValueError("--batch-size must be at least one")
            run_id = validate(
                connection, root, timeframe=args.timeframe, batch_size=args.batch_size,
                abs_tolerance=args.abs_tolerance, rel_tolerance=args.rel_tolerance,
                resume=args.command == "resume", force=args.force,
            )
            result = quality_report(connection, run_id, report_dir)
            discovery_report(connection, discovered, report_dir)
            inventory_report(connection, report_dir)
            performance_report(connection, report_dir)
            counts = connection.execute(
                "SELECT eligibility,count(*) FROM symbol_data_coverage "
                "GROUP BY eligibility ORDER BY eligibility"
            ).fetchall()
            _json_print({
                "run_id": run_id, "run_status": result["run"]["status"],
                "eligibility_counts": {key: value for key, value in counts},
                "reports": str(report_dir),
            })
            return 1 if result["run"]["status"] == "COMPLETED_WITH_ERRORS" else 0
        raise ValueError(f"Unknown command: {args.command}")
    finally:
        connection.close()
