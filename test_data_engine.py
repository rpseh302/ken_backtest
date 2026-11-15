from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

import duckdb
import pandas as pd

from backtester.data.data_discovery import discover
from backtester.data.parquet_reader import (
    ParquetSchemaError, create_normalized_temp_view, validate_required_columns,
)
from backtester.data.registry import connect
from backtester.data.recovery_manager import start_or_resume_run
from backtester.data.schema_validator import validate_schema
from backtester.data.segment_builder import merge_segments, segment_statistics
from backtester.data.validation_runner import validate


def _fixture_frame():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    base = (now_ms // 60_000 - 5) * 60_000
    opens = [base, base, base + 60_000, base + 60_000, base + 180_000,
             base + 240_000, base + 300_000]
    closes = [value + 59_999 for value in opens]
    frame = pd.DataFrame({
        "open_time": pd.to_datetime(opens, unit="ms", utc=True),
        "open": [10.0, 10.0, 10.0, 10.0, 10.0, 9.0, 10.0],
        "high": [11.0, 11.0, 11.0, 11.0, 11.0, 9.5, 11.0],
        "low": [9.0, 9.0, 9.0, 9.0, 9.0, 8.5, 9.0],
        "close": [10.5, 10.5, 10.5, 10.5, 10.5, 9.2, 10.5],
        "volume": [20.0, 20.0, 20.0, 20.0, 20.0, -1.0, 20.0],
        "close_time": pd.to_datetime(closes, unit="ms", utc=True),
        "quote_volume": [200.0, 200.0, 200.0, 200.0, 200.0, 10.0, 200.0],
        "count": [5, 5, 5, 5, 5, 1, 5],
        "taker_buy_volume": [10.0, 10.0, 10.0, 10.0, 10.0, 0.5, 10.0],
        "taker_buy_quote_volume": [100.0, 100.0, 100.0, 100.0, 100.0, 5.0, 100.0],
        "ignore": ["0"] * 7,
    })
    frame.loc[3, "close"] = 10.6
    return frame


class DataEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        path = self.root / "parquet" / "TESTUSDT" / "1m.parquet"
        path.parent.mkdir(parents=True)
        _fixture_frame().to_parquet(path, index=False)
        self.connection = connect(self.root / "catalog.duckdb", memory_limit="512MB", threads=1)

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def test_schema_aliases_and_missing_columns(self):
        resolved = validate_required_columns([
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "trade_count", "taker_buy_base_volume",
            "taker_buy_quote_volume", "ignore",
        ])
        self.assertEqual(resolved["count"], "trade_count")
        self.assertEqual(resolved["taker_buy_volume"], "taker_buy_base_volume")
        with self.assertRaises(ParquetSchemaError):
            validate_required_columns(["open_time"])
        self.assertEqual(validate_schema(next((self.root / "parquet").rglob("*.parquet")) )["issues"], [])

    def test_discovery_validation_duplicate_gaps_invalid_and_incomplete(self):
        files = discover(self.connection, self.root)
        self.assertEqual(len(files), 1)
        run_id = validate(self.connection, self.root, batch_size=2)
        codes = {row[0] for row in self.connection.execute(
            "SELECT DISTINCT issue_code FROM data_quality_issues WHERE run_id=?", [run_id]
        ).fetchall()}
        self.assertIn("CONFLICTING_DUPLICATE", codes)
        self.assertIn("EXACT_DUPLICATE", codes)
        self.assertIn("NEGATIVE_VOLUME", codes)
        self.assertIn("INCOMPLETE_CANDLE", codes)
        coverage = self.connection.execute(
            "SELECT expected_candles,observed_candles,missing_candles,eligibility "
            "FROM symbol_data_coverage WHERE symbol='TESTUSDT' AND timeframe='1m'"
        ).fetchone()
        self.assertEqual(coverage[2], 2)
        self.assertEqual(coverage[3], "INVALID")
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM valid_market_candles"
        ).fetchone()[0], 2)
        incomplete = _fixture_frame()["open_time"].max()
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM valid_market_candles WHERE open_time_ms=?",
            [int(incomplete.timestamp() * 1000)],
        ).fetchone()[0], 0)

    def test_short_contiguous_history_is_valid_and_gaps_are_partial(self):
        path = self.root / "parquet" / "TESTUSDT" / "1m.parquet"
        frame = _fixture_frame().iloc[[0, 4]].copy()
        frame["open_time"] = pd.to_datetime(
            [frame.iloc[0]["open_time"], frame.iloc[0]["open_time"] + pd.Timedelta(minutes=1)],
            utc=True,
        )
        frame["close_time"] = frame["open_time"] + pd.Timedelta(minutes=1) - pd.Timedelta(milliseconds=1)
        frame.to_parquet(path, index=False)
        discover(self.connection, self.root)
        validate(self.connection, self.root, batch_size=1, force=True)
        status = self.connection.execute(
            "SELECT eligibility FROM symbol_data_coverage WHERE symbol='TESTUSDT' AND timeframe='1m'"
        ).fetchone()[0]
        self.assertEqual(status, "VALID")
        frame = frame.iloc[[0, 0]].copy()
        frame.loc[:, "open_time"] = pd.to_datetime(
            [frame.iloc[0]["open_time"], frame.iloc[0]["open_time"] + pd.Timedelta(minutes=2)],
            utc=True,
        )
        frame.loc[:, "close_time"] = frame["open_time"] + pd.Timedelta(minutes=1) - pd.Timedelta(milliseconds=1)
        frame.to_parquet(path, index=False)
        discover(self.connection, self.root)
        validate(self.connection, self.root, batch_size=1, force=True)
        status = self.connection.execute(
            "SELECT missing_candles,eligibility FROM symbol_data_coverage "
            "WHERE symbol='TESTUSDT' AND timeframe='1m'"
        ).fetchone()
        self.assertEqual(status, (1, "PARTIAL"))

    def test_alignment_ohlc_and_volume_relationship_checks(self):
        path = self.root / "parquet" / "TESTUSDT" / "1m.parquet"
        frame = _fixture_frame().iloc[[0]].copy()
        frame["high"] = 9.0
        frame["taker_buy_volume"] = frame["volume"] + 1
        frame["quote_volume"] = 10.0
        frame["taker_buy_quote_volume"] = 11.0
        frame["count"] = -1
        frame["open_time"] = frame["open_time"] + pd.Timedelta(milliseconds=1)
        frame.to_parquet(path, index=False)
        discover(self.connection, self.root)
        run_id = validate(self.connection, self.root)
        codes = {row[0] for row in self.connection.execute(
            "SELECT DISTINCT issue_code FROM data_quality_issues WHERE run_id=?", [run_id]
        ).fetchall()}
        self.assertTrue({
            "TIMESTAMP_ALIGNMENT", "CLOSE_TIME_MISMATCH", "INVALID_OHLC",
            "NEGATIVE_TRADE_COUNT", "TAKER_VOLUME_EXCEEDS_TOTAL",
            "TAKER_QUOTE_VOLUME_EXCEEDS_TOTAL",
        }.issubset(codes))

    def test_timestamp_conversion_ignores_connection_timezone(self):
        path = next((self.root / "parquet").rglob("*.parquet"))
        connection = duckdb.connect()
        try:
            connection.execute("SET TimeZone='Asia/Tokyo'")
            create_normalized_temp_view(connection, path, batch_size=2)
            actual = connection.execute("SELECT min(open_time_ms) FROM current_data").fetchone()[0]
            expected = int(_fixture_frame()["open_time"].min().timestamp() * 1000)
            self.assertEqual(actual, expected)
        finally:
            connection.close()

    def test_rerun_is_idempotent_and_recovery_reuses_interrupted_run(self):
        discover(self.connection, self.root)
        first = validate(self.connection, self.root)
        memory = self.connection.execute(
            "SELECT peak_memory_bytes FROM data_quality_runs WHERE run_id=?", [first]
        ).fetchone()[0]
        self.assertGreater(memory, 0)
        issue_count = self.connection.execute(
            "SELECT count(*) FROM data_quality_issues WHERE run_id=?", [first]
        ).fetchone()[0]
        second = validate(self.connection, self.root)
        self.assertEqual(first, second)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM data_quality_runs").fetchone()[0], 1)
        resumed, is_complete = start_or_resume_run(self.connection, self.root, resume=True)
        self.assertEqual((resumed, is_complete), (first, True))
        self.connection.execute("UPDATE data_quality_runs SET status='INTERRUPTED' WHERE run_id=?", [first])
        self.connection.execute(
            "UPDATE datasets SET validation_status='PENDING' WHERE last_validation_run_id=?", [first]
        )
        resumed = validate(self.connection, self.root, batch_size=3, resume=True)
        self.assertEqual(resumed, first)
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM data_quality_issues WHERE run_id=?", [first]
            ).fetchone()[0],
            issue_count,
        )

    def test_segment_builder_merges_adjacent_and_preserves_partial_segments(self):
        segments = [(0, 60, 2), (120, 180, 2), (300, 300, 1)]
        merged = merge_segments(segments, 60)
        self.assertEqual(merged, [(0, 180, 4), (300, 300, 1)])
        stats = segment_statistics(merged, 60)
        self.assertEqual(stats["missing_candles"], 1)
        self.assertEqual(stats["coverage_ratio"], 5 / 6)


if __name__ == "__main__":
    unittest.main()
