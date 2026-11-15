from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow.parquet as pq

from backtester.data.config import COLUMN_ALIASES, TIMEFRAME_MS
from backtester.strategy.indicators import add_segment_indicators


def discover_market_files(data_root: str | Path) -> dict[str, dict[str, Path]]:
    root = Path(data_root) / "parquet"
    found: dict[str, dict[str, Path]] = {}
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*/*.parquet")):
        timeframe = path.stem
        if timeframe == "funding_rate" or timeframe not in TIMEFRAME_MS:
            continue
        found.setdefault(path.parent.name, {})[timeframe] = path
    return found


def _column_map(names) -> dict[str, str]:
    available = set(names)
    return {key: next((name for name in aliases if name in available), None)
            for key, aliases in COLUMN_ALIASES.items()}


def _to_epoch_ms(values: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(values):
        parsed = pd.to_datetime(values, utc=True, errors="coerce")
        converted = parsed.astype("int64") // 1_000_000
        return converted.where(parsed.notna())
    numeric = pd.to_numeric(values, errors="coerce")
    sample = numeric.dropna()
    if sample.empty:
        parsed = pd.to_datetime(values, utc=True, errors="coerce")
        return parsed.astype("int64") // 1_000_000
    magnitude = abs(float(sample.iloc[0]))
    if magnitude >= 1e17:
        return (numeric // 1_000_000).astype("Int64")
    if magnitude >= 1e14:
        return (numeric // 1_000).astype("Int64")
    if magnitude >= 1e11:
        return numeric.astype("Int64")
    parsed = pd.to_datetime(values, utc=True, errors="coerce")
    return parsed.astype("int64") // 1_000_000


def read_candles(path: str | Path) -> pd.DataFrame:
    frame = pq.read_table(path).to_pandas()
    columns = _column_map(frame.columns)
    required = ("open_time", "open", "high", "low", "close")
    missing = [name for name in required if not columns.get(name)]
    if missing:
        raise ValueError(f"{Path(path).name}: missing required columns: {', '.join(missing)}")
    result = pd.DataFrame()
    result["timestamp_ms"] = _to_epoch_ms(frame[columns["open_time"]])
    for name in ("open", "high", "low", "close", "volume"):
        source = columns.get(name)
        result[name] = pd.to_numeric(frame[source], errors="coerce") if source else 0.0
    result = result.dropna(subset=["timestamp_ms", "open", "high", "low", "close"])
    result["timestamp_ms"] = result["timestamp_ms"].astype("int64")
    result = result.sort_values("timestamp_ms").drop_duplicates("timestamp_ms", keep="first")
    result["datetime"] = pd.to_datetime(result["timestamp_ms"], unit="ms", utc=True)
    return result.reset_index(drop=True)


def read_runs(data_root: str | Path, db_path: str | Path | None = None) -> pd.DataFrame:
    root = Path(data_root)
    database = Path(db_path) if db_path else root / "backtester.duckdb"
    if database.is_file():
        try:
            con = duckdb.connect(str(database), read_only=True)
            try:
                tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
                if "backtest_runs" in tables:
                    rows = con.execute("""SELECT backtest_run_id, strategy_id, started_at_ms,
                        completed_at_ms, status, parameters_json, metrics_json
                        FROM backtest_runs ORDER BY started_at_ms DESC""").fetchdf()
                    if not rows.empty:
                        rows["run_id"] = rows["backtest_run_id"]
                        rows["metrics"] = rows["metrics_json"].map(_json_dict)
                        return rows
            finally:
                con.close()
        except (duckdb.Error, OSError):
            pass
    records = []
    report_root = root / "reports" / "backtests"
    if report_root.is_dir():
        for path in sorted(report_root.glob("*/performance.json"), reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                records.append({"run_id": payload.get("run_id", path.parent.name),
                                "strategy_id": "", "started_at_ms": None,
                                "completed_at_ms": None, "status": "COMPLETED",
                                "parameters_json": "{}", "metrics": payload.get("metrics", {}),
                                "report_dir": str(path.parent)})
            except (OSError, json.JSONDecodeError):
                continue
    return pd.DataFrame(records)


def _json_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def read_run_frames(data_root: str | Path, run_id: str, db_path: str | Path | None = None):
    root = Path(data_root)
    database = Path(db_path) if db_path else root / "backtester.duckdb"
    if database.is_file():
        try:
            con = duckdb.connect(str(database), read_only=True)
            try:
                tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
                if {"trades", "equity_curve"}.issubset(tables):
                    trades = con.execute("SELECT * FROM trades WHERE backtest_run_id = ? ORDER BY entry_time_ms", [run_id]).fetchdf()
                    equity = con.execute("SELECT * FROM equity_curve WHERE backtest_run_id = ? ORDER BY timestamp_ms", [run_id]).fetchdf()
                    return trades, equity
            finally:
                con.close()
        except (duckdb.Error, OSError):
            pass
    folder = root / "reports" / "backtests" / run_id
    trades_path, equity_path = folder / "trades.csv", folder / "equity_curve.csv"
    trades = pd.read_csv(trades_path) if trades_path.is_file() else pd.DataFrame()
    equity = pd.read_csv(equity_path) if equity_path.is_file() else pd.DataFrame()
    return trades, equity


def add_chart_indicators(candles: pd.DataFrame, fast: int = 20, slow: int = 50, atr: int = 14) -> pd.DataFrame:
    if candles.empty:
        return candles.copy()
    frame = candles.copy().reset_index(drop=True)
    timeframe_ms = None
    if len(frame) > 1:
        diffs = frame["timestamp_ms"].diff().dropna()
        timeframe_ms = int(diffs.median()) if not diffs.empty else None
    if timeframe_ms:
        frame["segment_index"] = (frame["timestamp_ms"].diff().fillna(timeframe_ms).ne(timeframe_ms)).cumsum()
    else:
        frame["segment_index"] = 0
    # A segment starts when the observed interval differs from the modal interval.
    if len(frame) > 1:
        diffs = frame["timestamp_ms"].diff()
        positive = diffs[diffs > 0]
        expected = int(positive.mode().iloc[0]) if not positive.empty else 0
        if expected:
            frame["segment_index"] = (diffs.ne(expected) & diffs.notna()).cumsum()
    return add_segment_indicators(frame, fast, slow, atr)
