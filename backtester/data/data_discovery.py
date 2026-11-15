from pathlib import Path

from .config import TIMEFRAME_MS
from .registry import register_dataset, update_view


def discover(connection, data_root):
    root = Path(data_root).resolve()
    parquet_root = root / "parquet"
    if not parquet_root.is_dir():
        raise FileNotFoundError(f"Parquet data directory not found: {parquet_root}")
    found = []
    for path in sorted(parquet_root.rglob("*.parquet")):
        timeframe = path.stem
        symbol = path.parent.name
        if timeframe not in TIMEFRAME_MS or not symbol:
            continue
        dataset_id = register_dataset(connection, path, symbol, timeframe)
        found.append({
            "dataset_id": dataset_id, "symbol": symbol, "timeframe": timeframe,
            "source_path": str(path.resolve()),
        })
    update_view(connection)
    if not found:
        raise FileNotFoundError(f"No supported timeframe Parquet files under {parquet_root}")
    return found
