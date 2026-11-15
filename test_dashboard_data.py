import tempfile
import unittest
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from dashboard.data import discover_market_files, read_candles, summarize_symbol_pnl


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

    def test_summarizes_realized_net_pnl_by_symbol(self):
        trades = pd.DataFrame({
            "symbol": ["BTCUSDT", "BTCUSDT", "ETHUSDT"],
            "net_pnl": [12.5, -2.5, 7.0],
        })
        result = summarize_symbol_pnl(trades).set_index("symbol")
        self.assertEqual(result.loc["BTCUSDT", "net_pnl"], 10.0)
        self.assertEqual(result.loc["BTCUSDT", "trade_count"], 2)
        self.assertEqual(result.loc["ETHUSDT", "net_pnl"], 7.0)

    def test_empty_trade_summary_is_empty(self):
        result = summarize_symbol_pnl(pd.DataFrame())
        self.assertEqual(list(result.columns), ["symbol", "net_pnl", "trade_count"])
        self.assertTrue(result.empty)


if __name__ == "__main__":
    unittest.main()
