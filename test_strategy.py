from dataclasses import replace
import unittest

import pandas as pd

from backtester.strategy.config import StrategyConfig
from backtester.strategy.engine import HOUR_MS, run_backtest
from backtester.strategy.indicators import add_segment_indicators


def _config(**changes):
    base = StrategyConfig(
        name="ema_20_50_atr", timeframe="1h", ema_fast=2, ema_slow=3,
        atr_period=2, atr_stop_multiplier=2.0, initial_capital=10_000.0,
        max_positions=10, allocation_per_position=0.1, leverage=1.0,
        margin_mode="isolated", fee_rate=0.001, slippage_bps=10.0, funding_enabled=True,
    )
    return replace(base, **changes)


def _frame(symbol, rows):
    values = []
    for index, row in enumerate(rows):
        open_time = index * HOUR_MS
        values.append({
            "symbol": symbol, "open_time_ms": open_time, "open": row[0],
            "high": row[1], "low": row[2], "close": row[3],
            "segment_index": row[4] if len(row) > 4 else 1,
            "signal": row[5] if len(row) > 5 else None,
            "atr": row[6] if len(row) > 6 else 1.0,
            "ema_fast": row[7] if len(row) > 7 else 2.0,
            "ema_slow": row[8] if len(row) > 8 else 1.0,
        })
    return pd.DataFrame(values)


class IndicatorTests(unittest.TestCase):
    def test_sma_seed_wilder_atr_and_segment_reset(self):
        frame = pd.DataFrame({
            "segment_index": [1, 1, 1, 2, 2],
            "high": [11, 12, 13, 30, 31],
            "low": [9, 10, 11, 28, 29],
            "close": [10, 11, 12, 29, 30],
        })
        result = add_segment_indicators(frame, 2, 3, 2)
        self.assertAlmostEqual(result.loc[1, "ema_fast"], 10.5)
        self.assertAlmostEqual(result.loc[2, "ema_slow"], 11.0)
        self.assertAlmostEqual(result.loc[1, "atr"], 2.0)
        self.assertTrue(pd.isna(result.loc[3, "ema_fast"]))
        self.assertTrue(pd.isna(result.loc[3, "atr"]))

    def test_crossovers_require_initialized_previous_values(self):
        frame = pd.DataFrame({
            "segment_index": [1] * 6,
            "high": [1, 1, 1, 2, 3, 4], "low": [1, 1, 1, 2, 3, 4],
            "close": [1, 1, 1, 2, 3, 4],
        })
        result = add_segment_indicators(frame, 2, 3, 2)
        self.assertEqual(result["signal"].dropna().tolist(), ["LONG"])


class ExecutionTests(unittest.TestCase):
    def test_long_stop_gap_fee_slippage_and_funding(self):
        candles = _frame("BTCUSDT", [
            (100, 101, 99, 100, 1, "LONG", 1.0),
            (100, 101, 99, 100, 1, None, 1.0),
            (95, 96, 94, 95, 1, None, 1.0),
        ])
        funding = {"BTCUSDT": [{
            "funding_time_ms": HOUR_MS + 1, "funding_rate": "0.01", "mark_price": "100",
        }]}
        trades, equity = run_backtest({"BTCUSDT": candles}, funding, _config())
        trade = trades.iloc[0]
        self.assertEqual(trade["direction"], "LONG")
        self.assertEqual(trade["exit_reason"], "ATR_STOP")
        self.assertAlmostEqual(trade["entry_price"], 100.1)
        self.assertAlmostEqual(trade["exit_price"], 94.905)
        self.assertLess(trade["funding_pnl"], 0)
        self.assertEqual(len(equity), 3)
        self.assertEqual(equity.iloc[-1]["open_positions"], 0)

    def test_reversal_closes_before_opposite_entry(self):
        candles = _frame("BTCUSDT", [
            (100, 101, 99, 100, 1, "LONG", 1.0),
            (100, 101, 99, 100, 1, "SHORT", 1.0),
            (100, 101, 99, 100, 1, None, 1.0),
            (100, 101, 99, 100, 1, None, 1.0),
        ])
        trades, _ = run_backtest({"BTCUSDT": candles}, {}, _config(funding_enabled=False))
        self.assertEqual(trades["direction"].tolist(), ["LONG", "SHORT"])
        self.assertEqual(trades.iloc[0]["exit_reason"], "EMA_CROSS")
        self.assertEqual(trades.iloc[1]["exit_reason"], "END_OF_BACKTEST")

    def test_portfolio_capacity_uses_spread_then_symbol_order(self):
        candles = {
            f"S{index:02}USDT": _frame(f"S{index:02}USDT", [
                (100, 101, 99, 100, 1, "LONG", 1.0, index + 1, 1.0),
                (100, 101, 99, 100, 1, None, 1.0),
                (100, 101, 99, 100, 1, None, 1.0),
            ])
            for index in range(11)
        }
        trades, _ = run_backtest(candles, {}, _config(funding_enabled=False, fee_rate=0.0))
        self.assertEqual(len(trades), 10)
        self.assertNotIn("S00USDT", set(trades["symbol"]))

    def test_gap_invalidates_without_fabricated_exit_fill(self):
        candles = _frame("BTCUSDT", [
            (100, 101, 99, 100, 1, "LONG", 1.0),
            (100, 101, 99, 100, 1, None, 1.0),
            (100, 101, 99, 100, 2, None, 1.0),
        ])
        candles.loc[2, "open_time_ms"] = 3 * HOUR_MS
        trades, _ = run_backtest({"BTCUSDT": candles}, {}, _config(funding_enabled=False))
        self.assertEqual(trades.iloc[0]["exit_reason"], "DATA_INVALIDATION")
        self.assertTrue(pd.isna(trades.iloc[0]["exit_price"]))

    def test_repeated_runs_are_numerically_reproducible(self):
        candles = _frame("BTCUSDT", [
            (100, 101, 99, 100, 1, "LONG", 1.0),
            (100, 101, 99, 100, 1, None, 1.0),
            (99, 100, 98, 99, 1, None, 1.0),
        ])
        first = run_backtest({"BTCUSDT": candles}, {}, _config(funding_enabled=False))
        second = run_backtest({"BTCUSDT": candles}, {}, _config(funding_enabled=False))
        pd.testing.assert_frame_equal(first[0], second[0])
        pd.testing.assert_frame_equal(first[1], second[1])


if __name__ == "__main__":
    unittest.main()
