import tempfile
import unittest
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from dashboard.data import discover_market_files, read_candles


class DashboardDataTests(unittest.TestCase):
    def test_discovers_market_timeframes_and_ignores_funding_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "parquet" / "BTCUSDT"
            root.mkdir(parents=True)
            (root / "1h.parquet").touch()
            (root / "funding_rate.parquet").touch()
            found = discover_market_files(temp)
            self.assertEqual(set(found["BTCUSDT"]), {"1h"})

    def test_reads_canonical_ohlcv_and_datetime(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "candles.parquet"
            frame = pd.DataFrame({
                "open_time": pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"], utc=True),
                "open": [10.0, 11.0], "high": [12.0, 13.0], "low": [9.0, 10.0],
                "close": [11.0, 12.0], "volume": [100.0, 120.0],
            })
            pq.write_table(pa.Table.from_pandas(frame), path)
            result = read_candles(path)
            self.assertEqual(len(result), 2)
            self.assertEqual(result.iloc[0]["close"], 11.0)
            self.assertEqual(str(result.iloc[0]["datetime"].tz), "UTC")


if __name__ == "__main__":
    unittest.main()
