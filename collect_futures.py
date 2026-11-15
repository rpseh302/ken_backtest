import argparse
import calendar
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import zipfile

from binance_historical_data import BinanceDataDumper
import pandas as pd


START_DATE = date(2026, 10, 1)
INTERVALS = ("1m", "5m", "15m", "1h", "4h")
BASE_URL = "https://data.binance.vision/data/futures/um"
EXCHANGE_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trade_count", "taker_buy_base_volume",
    "taker_buy_quote_volume", "ignore",
]
LOG = logging.getLogger("collector")


def fetch(url, allow_404=False):
    for attempt in range(4):
        try:
            request = Request(url, headers={"User-Agent": "usdt-futures-archive-collector/1.0"})
            with urlopen(request, timeout=60) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code == 404 and allow_404:
                return None
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise
            LOG.warning("HTTP %s, retrying %s", exc.code, url)
        except (URLError, TimeoutError, ConnectionError) as exc:
            if attempt == 3:
                raise
            LOG.warning("Network error %s, retrying %s", exc, url)
        time.sleep(2 ** attempt)
    raise RuntimeError(f"Unable to fetch {url}")


def get_symbols():
    payload = json.loads(fetch(EXCHANGE_URL))
    symbols = {}
    for item in payload["symbols"]:
        if item["quoteAsset"] == "USDT" and item["status"] == "TRADING":
            symbol = item["symbol"]
            if not symbol or not all(char.isalnum() or char == "_" for char in symbol):
                raise ValueError(f"Unexpected symbol: {symbol!r}")
            symbols[symbol] = datetime.fromtimestamp(
                item["onboardDate"] / 1000, tz=timezone.utc
            ).date()
    if not symbols:
        raise RuntimeError("Exchange returned no trading USDT symbols")
    return symbols


def month_end(day):
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def download_archive(dumper, symbol, interval, day, period):
    filename = dumper.create_filename(symbol, day, timeperiod_per_file=period, extension="zip")
    local_dir = Path(dumper.get_local_dir_to_data(symbol, timeperiod_per_file=period))
    csv_path = local_dir / filename.replace(".zip", ".csv")
    if csv_path.is_file():
        return csv_path
    url = f"{BASE_URL}/{period}/klines/{quote(symbol)}/{interval}/{quote(filename)}"
    content = fetch(url, allow_404=True)
    if content is None:
        return None
    checksum_text = fetch(url + ".CHECKSUM").decode("utf-8").strip().split()
    if not checksum_text or not re.fullmatch(r"[a-fA-F0-9]{64}", checksum_text[0]):
        raise ValueError(f"Invalid checksum file for {url}")
    if hashlib.sha256(content).hexdigest() != checksum_text[0].lower():
        raise ValueError(f"Checksum mismatch: {url}")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        members = archive.infolist()
        if len(members) != 1 or members[0].filename != csv_path.name:
            raise ValueError(f"Unexpected ZIP contents: {url}")
        local_dir.mkdir(parents=True, exist_ok=True)
        temp = csv_path.with_suffix(".csv.part")
        try:
            with archive.open(members[0]) as source, temp.open("wb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
            os.replace(temp, csv_path)
        finally:
            if temp.exists():
                temp.unlink()
    return csv_path


def select_files(dumper, symbol, interval, start, end):
    files = []
    missing = []
    current = start
    while current <= end:
        last = month_end(current)
        if current.day == 1 and last <= end:
            path = download_archive(dumper, symbol, interval, current, "monthly")
            if path is not None:
                files.append(path)
                current = last + timedelta(days=1)
                continue
            LOG.info("Monthly archive unavailable; trying daily: %s %s %s", symbol, interval, current)
        path = download_archive(dumper, symbol, interval, current, "daily")
        if path is None:
            missing.append(current.isoformat())
        else:
            files.append(path)
        current += timedelta(days=1)
    return files, missing


def read_frame(path):
    frame = pd.read_csv(path, header=None, dtype=str)
    if len(frame.columns) != len(COLUMNS):
        raise ValueError(f"Unexpected column count in {path}: {len(frame.columns)}")
    frame.columns = COLUMNS
    if not frame.empty and frame.iloc[0]["open_time"] in ("open_time", "Open time"):
        frame = frame.iloc[1:].copy()
    if frame.empty:
        raise ValueError(f"Empty archive: {path}")
    if frame[COLUMNS[:-1]].isna().any().any():
        raise ValueError(f"Missing candle fields in {path}")
    for column in ("open_time", "close_time"):
        frame[column] = pd.to_datetime(pd.to_numeric(frame[column], errors="raise"), unit="ms", utc=True)
    for column in COLUMNS[1:6] + COLUMNS[7:8] + COLUMNS[9:11]:
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    frame["trade_count"] = pd.to_numeric(frame["trade_count"], errors="raise").astype("int64")
    return frame


def collect_one(root, symbol, interval, onboard, start, end):
    start = max(start, onboard)
    if start > end:
        return {"symbol": symbol, "interval": interval, "rows": 0, "missing": []}
    dumper = BinanceDataDumper(
        path_dir_where_to_dump=str(root / "raw"), asset_class="um",
        data_type="klines", data_frequency=interval,
    )
    files, missing = select_files(dumper, symbol, interval, start, end)
    output = root / "parquet" / symbol / f"{interval}.parquet"
    if files:
        frame = pd.concat([read_frame(path) for path in files], ignore_index=True)
        lower = pd.Timestamp(start, tz="UTC")
        upper = pd.Timestamp(end + timedelta(days=1), tz="UTC")
        frame = frame[(frame["open_time"] >= lower) & (frame["open_time"] < upper)]
        if frame.duplicated("open_time").any():
            raise ValueError(f"Duplicate candle timestamps for {symbol} {interval}")
        frame = frame.sort_values("open_time").reset_index(drop=True)
        if frame.empty:
            raise ValueError(f"No candles in range for {symbol} {interval}")
        output.parent.mkdir(parents=True, exist_ok=True)
        temp = output.with_suffix(".parquet.part")
        try:
            frame.to_parquet(temp, index=False, compression="zstd")
            if not frame.equals(pd.read_parquet(temp)):
                raise ValueError(f"Parquet verification failed: {output}")
            os.replace(temp, output)
        finally:
            if temp.exists():
                temp.unlink()
        rows = len(frame)
    else:
        rows = 0
        if output.exists():
            raise RuntimeError(f"No archives retrieved; existing output not replaced: {output}")
    return {"symbol": symbol, "interval": interval, "rows": rows, "missing": missing}


def parse_args():
    parser = argparse.ArgumentParser(description="Download current USDT-M futures OHLCV archives")
    parser.add_argument("--output", type=Path, default=Path("data"))
    parser.add_argument("--start-date", type=date.fromisoformat, default=START_DATE)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--symbols", nargs="+", help="Limit to specific current trading symbols")
    parser.add_argument("--intervals", nargs="+", choices=INTERVALS, default=INTERVALS)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    latest = datetime.now(timezone.utc).date() - timedelta(days=2)
    if args.end_date is None:
        args.end_date = latest
    if args.end_date > latest:
        parser.error(f"end-date must be at most {latest} (UTC today minus two days)")
    if args.start_date > args.end_date:
        parser.error("start-date must not be after end-date")
    if args.workers < 1:
        parser.error("workers must be at least 1")
    if len(set(args.intervals)) != len(args.intervals):
        parser.error("intervals must not contain duplicates")
    return args


def main():
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="backslashreplace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True)
    args = parse_args()
    symbols = get_symbols()
    if args.symbols:
        unknown = set(args.symbols) - symbols.keys()
        if unknown:
            raise ValueError(f"Symbols not currently trading with USDT quote: {sorted(unknown)}")
        symbols = {symbol: symbols[symbol] for symbol in sorted(set(args.symbols))}
    LOG.info("Collecting %d symbols x %d intervals, %s through %s UTC",
             len(symbols), len(args.intervals), args.start_date, args.end_date)
    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        tasks = {
            executor.submit(collect_one, args.output, symbol, interval, onboard,
                            args.start_date, args.end_date): (symbol, interval)
            for symbol, onboard in sorted(symbols.items()) for interval in args.intervals
        }
        for future in as_completed(tasks):
            symbol, interval = tasks[future]
            try:
                result = future.result()
            except Exception as exc:
                LOG.exception("Failed %s %s", symbol, interval)
                errors.append({"symbol": symbol, "interval": interval, "error": str(exc)})
                continue
            results.append(result)
            LOG.info("Completed %s %s: %d rows, %d missing days (%d/%d)",
                     symbol, interval, result["rows"], len(result["missing"]),
                     len(results) + len(errors), len(tasks))
    report = {
        "start_date": args.start_date.isoformat(), "end_date": args.end_date.isoformat(),
        "symbol_count": len(symbols), "intervals": list(args.intervals),
        "results": sorted(results, key=lambda x: (x["symbol"], x["interval"])),
        "errors": errors,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    report_path = args.output / "report.json"
    temp = report_path.with_suffix(".json.part")
    temp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, report_path)
    missing_count = sum(len(item["missing"]) for item in results)
    LOG.info("Report: %s; errors=%d, missing days=%d", report_path, len(errors), missing_count)
    return 1 if errors or missing_count else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as exc:
        LOG.error("%s", exc)
        sys.exit(1)
