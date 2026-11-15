from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

import collect_funding_rates as collector


def _candles(path):
    table = pa.table({
        "open_time": pa.array([
            datetime(2026, 10, 1, tzinfo=timezone.utc),
            datetime(2026, 10, 1, 0, 1, tzinfo=timezone.utc),
        ], type=pa.timestamp("ms", tz="UTC")),
        "close_time": pa.array([
            datetime(2026, 10, 1, 0, 0, 59, 999000, tzinfo=timezone.utc),
            datetime(2026, 10, 1, 0, 1, 59, 999000, tzinfo=timezone.utc),
        ], type=pa.timestamp("ms", tz="UTC")),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


class FundingRateCollectorTests(unittest.TestCase):
    def test_discovers_actual_parquet_period(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _candles(root / "parquet" / "BTCUSDT" / "1m.parquet")
            pq.write_table(
                pa.table({"funding_time_ms": pa.array([1790812800000], type=pa.int64())}),
                root / "parquet" / "BTCUSDT" / "funding_rate.parquet",
            )
            self.assertEqual(
                collector.discovered_periods(root),
                {"BTCUSDT": (1790812800000, 1790812919999)},
            )

    def test_collects_raw_and_parquet_without_merging_candles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _candles(root / "parquet" / "BTCUSDT" / "1m.parquet")
            response = [{
                "symbol": "BTCUSDT", "fundingTime": 1790812800000,
                "fundingRate": "0.00010000", "markPrice": "100000.10",
                "rateType": "Regular",
            }]
            with patch.object(collector, "fetch", return_value=json.dumps(response).encode()):
                result = collector.collect_one(root, "BTCUSDT", 1790812800000, 1790812919999)
            self.assertEqual(result["records"], 1)
            raw = json.loads((root / "raw" / "futures" / "um" / "funding_rate" / "BTCUSDT.json").read_text())
            self.assertEqual(raw["records"][0]["funding_rate"], "0.00010000")
            table = pq.read_table(root / "parquet" / "BTCUSDT" / "funding_rate.parquet")
            self.assertEqual(table.num_rows, 1)
            self.assertEqual(table.column("funding_rate")[0].as_py(), "0.00010000")
            self.assertTrue((root / "parquet" / "BTCUSDT" / "1m.parquet").exists())

    def test_rejects_duplicate_funding_timestamps(self):
        response = [
            {"symbol": "BTCUSDT", "fundingTime": 1000, "fundingRate": "0.1"},
            {"symbol": "BTCUSDT", "fundingTime": 1000, "fundingRate": "0.2"},
        ]
        with patch.object(collector, "fetch", return_value=json.dumps(response).encode()):
            with self.assertRaisesRegex(ValueError, "Duplicate funding timestamps"):
                collector.fetch_funding_rates("BTCUSDT", 0, 2000)


if __name__ == "__main__":
    unittest.main()
