import json
from pathlib import Path
import tempfile
import unittest

import duckdb

from dashboard.jobs import get_backtest_readiness, list_jobs


class DashboardJobsTests(unittest.TestCase):
    def test_readiness_requires_validated_1h_segments(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "backtester.duckdb"
            con = duckdb.connect(str(db_path))
            con.execute("CREATE TABLE symbol_data_coverage(symbol VARCHAR, timeframe VARCHAR, eligibility VARCHAR)")
            con.execute("CREATE TABLE coverage_segments(symbol VARCHAR, timeframe VARCHAR)")
            con.execute("INSERT INTO symbol_data_coverage VALUES ('BTCUSDT','1h','VALID')")
            con.execute("INSERT INTO coverage_segments VALUES ('BTCUSDT','1h')")
            con.close()
            result = get_backtest_readiness(db_path)
            self.assertTrue(result["ready"])
            self.assertEqual(result["symbols"], 1)
            self.assertEqual(result["segments"], 1)

    def test_readiness_reports_missing_catalog(self):
        with tempfile.TemporaryDirectory() as folder:
            result = get_backtest_readiness(Path(folder) / "missing.duckdb")
            self.assertFalse(result["ready"])
            self.assertIn("카탈로그", result["reason"])

    def test_list_jobs_reads_newest_job_records(self):
        with tempfile.TemporaryDirectory() as folder:
            job_dir = Path(folder) / "reports" / "gui_jobs" / "job-1"
            job_dir.mkdir(parents=True)
            (job_dir / "job.json").write_text(json.dumps({
                "job_id": "job-1", "status": "COMPLETED", "created_at": "2026-11-15T10:00:00+00:00"
            }), encoding="utf-8")
            jobs = list_jobs(folder)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["job_id"], "job-1")


if __name__ == "__main__":
    unittest.main()
