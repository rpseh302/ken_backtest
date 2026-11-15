import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
from urllib.parse import quote, urlencode

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from collect_futures import EXCHANGE_URL, fetch


FUNDING_RATE_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
KLINE_FILENAMES = frozenset({"1m.parquet", "5m.parquet", "15m.parquet", "1h.parquet", "4h.parquet"})
FUNDING_COLUMNS = (
    "symbol", "funding_time", "funding_time_ms", "funding_rate", "mark_price",
    "rate_type", "source_url",
)
LOG = logging.getLogger("funding-rate-collector")


def _epoch_ms(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)
    if isinstance(value, int):
        return value
    raise TypeError(f"Unsupported timestamp value: {value!r}")


def _column_statistics(parquet_file, column_name):
    column_index = parquet_file.schema_arrow.get_field_index(column_name)
    if column_index < 0:
        raise ValueError(f"Missing {column_name} column in source Parquet")
    minimum = maximum = None
    for row_group in range(parquet_file.metadata.num_row_groups):
        statistics = parquet_file.metadata.row_group(row_group).column(column_index).statistics
        if statistics is None or not statistics.has_min_max:
            return None
        current_min = _epoch_ms(statistics.min)
        current_max = _epoch_ms(statistics.max)
        minimum = current_min if minimum is None else min(minimum, current_min)
        maximum = current_max if maximum is None else max(maximum, current_max)
    return minimum, maximum


def parquet_period(path, batch_size=65_536):
    parquet_file = pq.ParquetFile(path)
    open_statistics = _column_statistics(parquet_file, "open_time")
    close_statistics = _column_statistics(parquet_file, "close_time")
    if open_statistics and close_statistics:
        return open_statistics[0], close_statistics[1]

    first_open_ms = None
    last_close_ms = None
    for batch in parquet_file.iter_batches(
        batch_size=batch_size, columns=["open_time", "close_time"],
    ):
        open_time = pc.min(batch.column("open_time")).as_py()
        close_time = pc.max(batch.column("close_time")).as_py()
        if open_time is not None:
            first_open_ms = min(first_open_ms, _epoch_ms(open_time)) if first_open_ms else _epoch_ms(open_time)
        if close_time is not None:
            last_close_ms = max(last_close_ms, _epoch_ms(close_time)) if last_close_ms else _epoch_ms(close_time)
    if first_open_ms is None or last_close_ms is None:
        raise ValueError(f"Empty Parquet file: {path}")
    return first_open_ms, last_close_ms


def discovered_periods(data_root):
    periods = {}
    for symbol_directory in sorted((data_root / "parquet").iterdir()):
        if not symbol_directory.is_dir():
            continue
        files = sorted(
            path for path in symbol_directory.glob("*.parquet")
            if path.name in KLINE_FILENAMES
        )
        if not files:
            continue
        start_ms = end_ms = None
        for path in files:
            first_ms, last_ms = parquet_period(path)
            start_ms = first_ms if start_ms is None else min(start_ms, first_ms)
            end_ms = last_ms if end_ms is None else max(end_ms, last_ms)
        periods[symbol_directory.name] = (start_ms, end_ms)
    if not periods:
        raise FileNotFoundError(f"No Parquet data found below {data_root / 'parquet'}")
    return periods


def perpetual_symbols():
    payload = json.loads(fetch(EXCHANGE_URL))
    return {
        item["symbol"]: item["contractType"]
        for item in payload["symbols"]
        if item.get("symbol") and item.get("quoteAsset") == "USDT"
    }


def fetch_funding_rates(symbol, start_ms, end_ms):
    records = []
    cursor_ms = start_ms
    while cursor_ms <= end_ms:
        query = urlencode({
            "symbol": symbol, "startTime": cursor_ms, "endTime": end_ms, "limit": 1000,
        })
        source_url = f"{FUNDING_RATE_URL}?{query}"
        page = json.loads(fetch(source_url))
        if not isinstance(page, list):
            raise ValueError(f"Unexpected funding-rate response for {symbol}")
        if not page:
            break
        for item in page:
            if item.get("symbol") != symbol:
                raise ValueError(f"Unexpected funding-rate symbol for {symbol}: {item!r}")
            funding_time_ms = int(item["fundingTime"])
            if funding_time_ms < start_ms or funding_time_ms > end_ms:
                raise ValueError(f"Out-of-range funding event for {symbol}: {funding_time_ms}")
            records.append({
                "symbol": symbol,
                "funding_time": datetime.fromtimestamp(funding_time_ms / 1000, tz=timezone.utc),
                "funding_time_ms": funding_time_ms,
                "funding_rate": str(item["fundingRate"]),
                "mark_price": str(item.get("markPrice", "")),
                "rate_type": str(item.get("rateType", "")),
                "source_url": source_url,
            })
        next_cursor_ms = int(page[-1]["fundingTime"]) + 1
        if next_cursor_ms <= cursor_ms:
            raise ValueError(f"Non-advancing funding-rate page for {symbol}")
        cursor_ms = next_cursor_ms
        if len(page) < 1000:
            break

    duplicate_times = {
        item["funding_time_ms"] for item in records
        if sum(other["funding_time_ms"] == item["funding_time_ms"] for other in records) > 1
    }
    if duplicate_times:
        raise ValueError(f"Duplicate funding timestamps for {symbol}: {sorted(duplicate_times)}")
    return records


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=lambda value: value.isoformat()),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_parquet(path, records):
    schema = pa.schema([
        pa.field("symbol", pa.string()),
        pa.field("funding_time", pa.timestamp("ms", tz="UTC")),
        pa.field("funding_time_ms", pa.int64()),
        pa.field("funding_rate", pa.string()),
        pa.field("mark_price", pa.string()),
        pa.field("rate_type", pa.string()),
        pa.field("source_url", pa.string()),
    ])
    table = pa.Table.from_pylist(records, schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".parquet.part")
    try:
        pq.write_table(table, temporary, compression="zstd")
        verified_schema = pq.read_schema(temporary)
        verified_rows = pq.read_metadata(temporary).num_rows
        if verified_rows != len(records) or verified_schema != schema:
            raise ValueError(f"Parquet verification failed: {path}")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def collect_one(data_root, symbol, start_ms, end_ms):
    records = fetch_funding_rates(symbol, start_ms, end_ms)
    raw_path = data_root / "raw" / "futures" / "um" / "funding_rate" / f"{quote(symbol)}.json"
    parquet_path = data_root / "parquet" / symbol / "funding_rate.parquet"
    _write_json(raw_path, {
        "symbol": symbol, "requested_start_ms": start_ms, "requested_end_ms": end_ms,
        "records": records,
    })
    _write_parquet(parquet_path, records)
    return {
        "symbol": symbol, "requested_start_ms": start_ms, "requested_end_ms": end_ms,
        "records": len(records), "raw_path": str(raw_path), "parquet_path": str(parquet_path),
    }


def existing_result(data_root, symbol, start_ms, end_ms):
    raw_path = data_root / "raw" / "futures" / "um" / "funding_rate" / f"{quote(symbol)}.json"
    parquet_path = data_root / "parquet" / symbol / "funding_rate.parquet"
    if not raw_path.is_file() or not parquet_path.is_file():
        return None
    schema = pq.read_schema(parquet_path)
    if tuple(schema.names) != FUNDING_COLUMNS:
        raise ValueError(f"Unexpected funding-rate schema: {parquet_path}")
    return {
        "symbol": symbol, "requested_start_ms": start_ms, "requested_end_ms": end_ms,
        "records": pq.read_metadata(parquet_path).num_rows,
        "raw_path": str(raw_path), "parquet_path": str(parquet_path),
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download USD-M perpetual funding rates for locally downloaded Parquet periods",
    )
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--symbols", nargs="+", help="Limit collection to downloaded symbols")
    return parser.parse_args()


def main():
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="backslashreplace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True)
    args = parse_args()
    if args.workers < 1:
        raise ValueError("--workers must be at least one")

    all_periods = discovered_periods(args.data_root)
    periods = all_periods
    if args.symbols:
        unknown_symbols = sorted(set(args.symbols) - set(periods))
        if unknown_symbols:
            raise ValueError(f"Symbols have no downloaded Parquet data: {unknown_symbols}")
        periods = {symbol: periods[symbol] for symbol in sorted(set(args.symbols))}
    contracts = perpetual_symbols()
    missing_contracts = sorted(set(all_periods) - set(contracts))
    if missing_contracts:
        raise ValueError(f"Missing exchange contract metadata: {missing_contracts}")
    all_perpetual = {
        symbol: period for symbol, period in all_periods.items()
        if contracts[symbol] == "PERPETUAL"
    }
    perpetual = {
        symbol: period for symbol, period in periods.items()
        if contracts[symbol] == "PERPETUAL"
    }
    skipped = sorted(symbol for symbol in all_periods if contracts[symbol] != "PERPETUAL")
    LOG.info("Downloading funding rates for %d perpetual symbols; skipping %d non-perpetual symbols",
             len(perpetual), len(skipped))
    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        tasks = {
            executor.submit(collect_one, args.data_root, symbol, *period): symbol
            for symbol, period in perpetual.items()
        }
        for index, future in enumerate(as_completed(tasks), 1):
            symbol = tasks[future]
            try:
                result = future.result()
            except Exception as exc:
                LOG.exception("Failed %s", symbol)
                errors.append({"symbol": symbol, "error": str(exc)})
            else:
                results.append(result)
                LOG.info("Completed %s: %d funding records (%d/%d)",
                         symbol, result["records"], index, len(tasks))
    completed = []
    for symbol, period in all_perpetual.items():
        result = existing_result(args.data_root, symbol, *period)
        if result is not None:
            completed.append(result)
    report = {
        "source": FUNDING_RATE_URL, "symbols_discovered": len(all_periods),
        "perpetual_symbols": len(all_perpetual), "symbols_requested": len(perpetual),
        "skipped_non_perpetual_symbols": skipped,
        "results": sorted(completed, key=lambda item: item["symbol"]),
        "errors": sorted(errors, key=lambda item: item["symbol"]),
    }
    _write_json(args.data_root / "funding_rate_report.json", report)
    LOG.info("Report: %s; errors=%d", args.data_root / "funding_rate_report.json", len(errors))
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        LOG.error("%s", exc)
        sys.exit(1)
