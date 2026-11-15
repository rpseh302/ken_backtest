from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    timeframe: str
    ema_fast: int
    ema_slow: int
    atr_period: int
    atr_stop_multiplier: float
    initial_capital: float
    max_positions: int
    allocation_per_position: float
    leverage: float
    margin_mode: str
    fee_rate: float
    slippage_bps: float
    funding_enabled: bool

    def validate(self):
        if self.timeframe != "1h":
            raise ValueError("ema_20_50_atr supports only 1h candles")
        if self.ema_fast < 1 or self.ema_slow < self.ema_fast:
            raise ValueError("EMA periods must satisfy 1 <= fast <= slow")
        if self.atr_period < 1 or self.atr_stop_multiplier <= 0:
            raise ValueError("ATR configuration must be positive")
        if self.initial_capital <= 0 or self.max_positions < 1:
            raise ValueError("Portfolio capital and position limit must be positive")
        if not 0 < self.allocation_per_position <= 1 or self.leverage <= 0:
            raise ValueError("Allocation must be in (0, 1] and leverage must be positive")
        if self.margin_mode != "isolated":
            raise ValueError("Only isolated margin is supported")
        if self.fee_rate < 0 or self.slippage_bps < 0:
            raise ValueError("Fee rate and slippage must be non-negative")


def load_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    strategy = payload["strategy"]
    portfolio = payload["portfolio"]
    execution = payload["execution"]
    result = StrategyConfig(
        name=strategy["name"], timeframe=strategy["timeframe"],
        ema_fast=int(strategy["ema_fast"]), ema_slow=int(strategy["ema_slow"]),
        atr_period=int(strategy["atr_period"]),
        atr_stop_multiplier=float(strategy["atr_stop_multiplier"]),
        initial_capital=float(portfolio["initial_capital"]),
        max_positions=int(portfolio["max_positions"]),
        allocation_per_position=float(portfolio["allocation_per_position"]),
        leverage=float(portfolio["leverage"]), margin_mode=portfolio["margin_mode"],
        fee_rate=float(execution["fee_rate"]), slippage_bps=float(execution["slippage_bps"]),
        funding_enabled=bool(execution["funding_enabled"]),
    )
    result.validate()
    return result
