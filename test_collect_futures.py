import hashlib
import io
import json
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from binance_historical_data import BinanceDataDumper
import pandas as pd

import collect_futures as collector


class CollectorTests(unittest.TestCase):
    def test_selects_current_usdt_only_and_accepts_unicode(self):
        response = {"symbols": [
            {"symbol": "BTCUSDT", "quoteAsset": "USDT", "status": "TRADING", "onboardDate": 0},
            {"symbol": "币USDT", "quoteAsset": "USDT", "status": "TRADING", "onboardDate": 0},
            {"symbol": "OLDUSDT", "quoteAsset": "USDT", "status": "SETTLING", "onboardDate": 0},
            {"symbol": "BTCUSDC", "quoteAsset": "USDC", "status": "TRADING", "onboardDate": 0},
        ]}
        with patch.object(collector, "fetch", return_value=json.dumps(response).encode()):
            self.assertEqual(set(collector.get_symbols()), {"BTCUSDT", "币USDT"})

    def test_monthly_fallback_and_exact_end(self):
        calls = []

        def fake_download(*args):
            day, period = args[-2:]
            calls.append((day, period))
            if period == "monthly" or day == date(2026, 10, 2):
                return None
            return Path(day.isoformat())

        with patch.object(collector, "download_archive", side_effect=fake_download):
            files, missing = collector.select_files(None, "BTCUSDT", "1m",
                                                    date(2026, 10, 1), date(2026, 11, 2))
        self.assertEqual(calls[0], (date(2026, 10, 1), "monthly"))
        self.assertEqual(calls[-1], (date(2026, 11, 2), "daily"))
        self.assertEqual(len(files), 32)
        self.assertEqual(missing, ["2026-10-02"])

    def test_archive_checksum_and_safe_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            dumper = BinanceDataDumper(tmp, asset_class="um", data_type="klines", data_frequency="1m")
            day = date(2026, 10, 1)
            name = dumper.create_filename("BTCUSDT", day, timeperiod_per_file="daily", extension="csv")
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr(name, "sample\n")
            content = buffer.getvalue()
            checksum = hashlib.sha256(content).hexdigest().encode()
            with patch.object(collector, "fetch", side_effect=[content, checksum]):
                path = collector.download_archive(dumper, "BTCUSDT", "1m", day, "daily")
            self.assertEqual(path.read_text(), "sample\n")
        with tempfile.TemporaryDirectory() as tmp:
            dumper = BinanceDataDumper(tmp, asset_class="um", data_type="klines", data_frequency="1m")
            with patch.object(collector, "fetch", side_effect=[content, b"0" * 64]):
                with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                    collector.download_archive(dumper, "BTCUSDT", "1m", day, "daily")

    def test_dataframe_timestamps_and_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candle.csv"
            path.write_text(
                ",".join(collector.COLUMNS) + "\n"
                "1790812800000,1,2,0.5,1.5,10,1790812859999,15,3,4,6,0\n",
                encoding="utf-8",
            )
            frame = collector.read_frame(path)
            self.assertEqual(frame["open_time"].iloc[0], pd.Timestamp("2026-10-01", tz="UTC"))
            self.assertEqual(frame["trade_count"].iloc[0], 3)


if __name__ == "__main__":
    unittest.main()
