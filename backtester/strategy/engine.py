from dataclasses import asdict, dataclass
from pathlib import Path
import uuid

import pandas as pd
import pyarrow.parquet as pq

from .indicators import add_segment_indicators


HOUR_MS = 3_600_000


@dataclass
class Position:
    symbol: str
    direction: str
    entry_time_ms: int
    entry_signal_time_ms: int
    entry_price: float
    quantity: float
    entry_notional: float
    initial_atr: float
    initial_stop_price: float
    segment_index: int
    entry_fee: float
    entry_slippage_cost: float
    funding_pnl: float = 0.0


def adverse_fill(price, direction, is_entry, slippage_bps):
    multiplier = slippage_bps / 10_000
    if (direction == "LONG") == is_entry:
        return price * (1 + multiplier)
    return price * (1 - multiplier)


def prepare_candles(frame, segments, config):
    pieces = []
    for segment_index, start_ms, end_ms, candle_count in segments:
        piece = frame[
            (frame["open_time_ms"] >= start_ms) & (frame["open_time_ms"] <= end_ms)
        ].copy()
        if len(piece) != candle_count:
            raise ValueError(f"Coverage segment {segment_index} does not match valid candles")
        piece["segment_index"] = segment_index
        pieces.append(piece)
    if not pieces:
        return pd.DataFrame()
    return add_segment_indicators(
        pd.concat(pieces, ignore_index=True).sort_values("open_time_ms"),
        config.ema_fast, config.ema_slow, config.atr_period,
    )


def load_funding(data_root, symbol):
    path = Path(data_root) / "parquet" / symbol / "funding_rate.parquet"
    if not path.is_file():
        return []
    return pq.read_table(path).to_pandas().to_dict("records")


def _position_unrealized(position, close):
    sign = 1 if position.direction == "LONG" else -1
    return (close - position.entry_price) * position.quantity * sign


def run_backtest(candles_by_symbol, funding_by_symbol, config):
    """Run a deterministic synchronized backtest over already validated candle segments."""
    config.validate()
    positions = {}
    pending = {}
    trades = []
    equity_rows = []
    cash = config.initial_capital
    realized_pnl = 0.0
    fee_total = 0.0
    funding_total = 0.0
    slippage_total = 0.0
    latest_close = {}
    peak_equity = config.initial_capital

    candles_at_time = {}
    for symbol, frame in candles_by_symbol.items():
        for row in frame.to_dict("records"):
            candles_at_time.setdefault(int(row["open_time_ms"]), []).append(row)
    funding_by_candle = {}
    if config.funding_enabled:
        for symbol, records in funding_by_symbol.items():
            for record in records:
                funding_time = int(record["funding_time_ms"])
                funding_by_candle.setdefault(funding_time // HOUR_MS * HOUR_MS, []).append(
                    (funding_time, symbol, record)
                )

    def total_equity():
        return cash + sum(_position_unrealized(position, latest_close[position.symbol])
                          for position in positions.values() if position.symbol in latest_close)

    def close_position(symbol, timestamp_ms, raw_price, reason, allow_missing_price=False):
        nonlocal cash, realized_pnl, fee_total, slippage_total
        position = positions.pop(symbol)
        if raw_price is None and allow_missing_price:
            trades.append({
                **asdict(position), "trade_id": str(uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{position.symbol}:{position.direction}:{position.entry_time_ms}:{timestamp_ms}",
                )), "exit_time_ms": timestamp_ms,
                "exit_price": None, "exit_reason": reason, "gross_pnl": None,
                "exit_fee": None, "slippage_cost": position.entry_slippage_cost,
                "net_pnl": None, "return_on_notional": None,
                "holding_duration_ms": timestamp_ms - position.entry_time_ms,
            })
            return
        fill = adverse_fill(raw_price, position.direction, False, config.slippage_bps)
        gross = _position_unrealized(position, fill)
        exit_fee = abs(fill * position.quantity) * config.fee_rate
        exit_slippage = abs(raw_price - fill) * position.quantity
        net = gross - position.entry_fee - exit_fee + position.funding_pnl
        cash += gross - exit_fee
        realized_pnl += gross
        fee_total += exit_fee
        slippage_total += exit_slippage
        trades.append({
            **asdict(position), "trade_id": str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{position.symbol}:{position.direction}:{position.entry_time_ms}:{timestamp_ms}",
            )), "exit_time_ms": timestamp_ms,
            "exit_price": fill, "exit_reason": reason, "gross_pnl": gross,
            "exit_fee": exit_fee, "slippage_cost": position.entry_slippage_cost + exit_slippage,
            "net_pnl": net, "return_on_notional": net / position.entry_notional,
            "holding_duration_ms": timestamp_ms - position.entry_time_ms,
        })

    def enter(candidate, timestamp_ms, open_price):
        nonlocal cash, fee_total, slippage_total
        if candidate["symbol"] in positions or len(positions) >= config.max_positions:
            return
        equity = total_equity()
        notional = equity * config.allocation_per_position
        fill = adverse_fill(open_price, candidate["direction"], True, config.slippage_bps)
        quantity = notional / fill
        margin = notional / config.leverage
        fee = notional * config.fee_rate
        used_margin = sum(position.entry_notional / config.leverage for position in positions.values())
        if cash - used_margin < margin + fee:
            return
        stop = fill - config.atr_stop_multiplier * candidate["atr"]
        if candidate["direction"] == "SHORT":
            stop = fill + config.atr_stop_multiplier * candidate["atr"]
        positions[candidate["symbol"]] = Position(
            symbol=candidate["symbol"], direction=candidate["direction"],
            entry_time_ms=timestamp_ms, entry_signal_time_ms=candidate["signal_time_ms"],
            entry_price=fill, quantity=quantity, entry_notional=notional,
            initial_atr=candidate["atr"], initial_stop_price=stop,
            segment_index=candidate["segment_index"], entry_fee=fee,
            entry_slippage_cost=abs(fill - open_price) * quantity,
        )
        cash -= fee
        fee_total += fee
        slippage_total += abs(fill - open_price) * quantity

    for timestamp_ms in sorted(candles_at_time):
        current_rows = {row["symbol"]: row for row in candles_at_time[timestamp_ms]}
        for symbol, row in current_rows.items():
            existing = positions.get(symbol)
            if existing and existing.segment_index != row["segment_index"]:
                close_position(symbol, timestamp_ms, None, "DATA_INVALIDATION", allow_missing_price=True)

        scheduled = pending.pop(timestamp_ms, [])
        for candidate in scheduled:
            if candidate["action"] == "CLOSE" and candidate["symbol"] in positions:
                close_position(candidate["symbol"], timestamp_ms, current_rows[candidate["symbol"]]["open"], "EMA_CROSS")
        entries = [candidate for candidate in scheduled if candidate["action"] == "ENTRY"]
        for candidate in sorted(entries, key=lambda item: (-item["spread_ratio"], item["symbol"])):
            row = current_rows.get(candidate["symbol"])
            if row is not None:
                enter(candidate, timestamp_ms, row["open"])

        for funding_time, symbol, funding in sorted(funding_by_candle.get(timestamp_ms, [])):
            if timestamp_ms <= funding_time <= timestamp_ms + HOUR_MS - 1:
                position = positions.get(symbol)
                if position is None:
                    continue
                mark_price = float(funding["mark_price"])
                payment = position.quantity * mark_price * float(funding["funding_rate"])
                if position.direction == "LONG":
                    payment = -payment
                position.funding_pnl += payment
                cash += payment
                funding_total += payment

        for symbol in sorted(current_rows):
            row = current_rows[symbol]
            latest_close[symbol] = row["close"]
            position = positions.get(symbol)
            if not position:
                continue
            stop_hit = (
                position.direction == "LONG" and row["low"] <= position.initial_stop_price
            ) or (
                position.direction == "SHORT" and row["high"] >= position.initial_stop_price
            )
            if stop_hit:
                raw = row["open"]
                if position.direction == "LONG":
                    raw = min(raw, position.initial_stop_price)
                else:
                    raw = max(raw, position.initial_stop_price)
                close_position(symbol, timestamp_ms, raw, "ATR_STOP")

        next_time = timestamp_ms + HOUR_MS
        for symbol in sorted(current_rows):
            row = current_rows[symbol]
            if pd.isna(row["signal"]) or next_time not in candles_at_time or symbol not in {
                item["symbol"] for item in candles_at_time[next_time]
            }:
                continue
            direction = row["signal"]
            existing = positions.get(symbol)
            if existing and existing.direction == direction:
                continue
            if existing:
                pending.setdefault(next_time, []).append({"symbol": symbol, "action": "CLOSE"})
            pending.setdefault(next_time, []).append({
                "symbol": symbol, "action": "ENTRY", "direction": direction,
                "signal_time_ms": timestamp_ms, "atr": float(row["atr"]),
                "segment_index": int(row["segment_index"]),
                "spread_ratio": abs(float(row["ema_fast"]) - float(row["ema_slow"])) / float(row["close"]),
            })

        unrealized = sum(_position_unrealized(position, latest_close[position.symbol])
                         for position in positions.values() if position.symbol in latest_close)
        used_margin = sum(position.entry_notional / config.leverage for position in positions.values())
        equity_value = cash + unrealized
        peak_equity = max(peak_equity, equity_value)
        equity_rows.append({
            "timestamp_ms": timestamp_ms + HOUR_MS - 1, "equity": equity_value,
            "cash": cash, "available_balance": cash - used_margin, "unrealized_pnl": unrealized,
            "realized_pnl": realized_pnl, "open_positions": len(positions),
            "long_exposure": sum(position.entry_notional for position in positions.values()
                                 if position.direction == "LONG"),
            "short_exposure": sum(position.entry_notional for position in positions.values()
                                  if position.direction == "SHORT"),
            "total_exposure": sum(position.entry_notional for position in positions.values()),
            "used_margin": used_margin,
            "total_fees": fee_total, "funding_pnl": funding_total,
            "slippage_cost": slippage_total, "drawdown": equity_value / peak_equity - 1,
        })

    for symbol, position in list(positions.items()):
        close_position(symbol, max(row["timestamp_ms"] for row in equity_rows), latest_close[symbol], "END_OF_BACKTEST")
    if equity_rows:
        final = equity_rows[-1]
        final.update({
            "equity": cash, "cash": cash, "available_balance": cash,
            "unrealized_pnl": 0.0, "realized_pnl": realized_pnl, "open_positions": 0,
            "long_exposure": 0.0, "short_exposure": 0.0, "total_exposure": 0.0,
            "used_margin": 0.0, "total_fees": fee_total,
            "funding_pnl": funding_total, "slippage_cost": slippage_total,
            "drawdown": cash / peak_equity - 1,
        })
    return pd.DataFrame(trades), pd.DataFrame(equity_rows)
