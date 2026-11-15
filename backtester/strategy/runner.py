import json
from pathlib import Path
import uuid

import pandas as pd

from ..data.data_discovery import discover
from ..data.registry import now_ms
from .engine import load_funding, prepare_candles, run_backtest


def _write_frame(path, frame):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    try:
        frame.to_csv(temporary, index=False)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _metrics(trades, equity, initial_capital):
    closed = trades.dropna(subset=["net_pnl"]) if not trades.empty else trades
    final_equity = float(equity.iloc[-1]["equity"]) if not equity.empty else initial_capital
    duration_ms = int(equity.iloc[-1]["timestamp_ms"] - equity.iloc[0]["timestamp_ms"]) if len(equity) > 1 else 0
    returns = equity["equity"].pct_change().dropna() if len(equity) > 1 else pd.Series(dtype=float)
    peak = equity["equity"].cummax() if not equity.empty else pd.Series(dtype=float)
    drawdown = equity["equity"] / peak - 1 if not equity.empty else pd.Series(dtype=float)
    gains = closed.loc[closed["net_pnl"] > 0, "net_pnl"].sum()
    losses = -closed.loc[closed["net_pnl"] < 0, "net_pnl"].sum()
    return {
        "initial_capital": initial_capital, "final_equity": final_equity,
        "total_return": final_equity / initial_capital - 1,
        "annualized_return": (final_equity / initial_capital) ** (365.25 * 24 * 3_600_000 / duration_ms) - 1
        if duration_ms else None,
        "maximum_drawdown": float(drawdown.min()) if not drawdown.empty else 0.0,
        "sharpe_ratio": float(returns.mean() / returns.std() * (24 * 365) ** 0.5)
        if len(returns) > 1 and returns.std() else None,
        "sortino_ratio": float(returns.mean() / returns[returns < 0].std() * (24 * 365) ** 0.5)
        if (returns < 0).sum() > 1 and returns[returns < 0].std() else None,
        "profit_factor": float(gains / losses) if losses else None,
        "total_trades": int(len(closed)),
        "win_rate": float((closed["net_pnl"] > 0).mean()) if len(closed) else None,
        "average_win": float(closed.loc[closed["net_pnl"] > 0, "net_pnl"].mean())
        if (closed["net_pnl"] > 0).any() else None,
        "average_loss": float(closed.loc[closed["net_pnl"] < 0, "net_pnl"].mean())
        if (closed["net_pnl"] < 0).any() else None,
        "average_trade_return": float(closed["return_on_notional"].mean()) if len(closed) else None,
        "average_holding_duration_ms": float(closed["holding_duration_ms"].mean()) if len(closed) else None,
        "long_pnl": float(closed.loc[closed["direction"] == "LONG", "net_pnl"].sum()),
        "short_pnl": float(closed.loc[closed["direction"] == "SHORT", "net_pnl"].sum()),
        "total_fees": float(closed["entry_fee"].sum() + closed["exit_fee"].sum()) if len(closed) else 0.0,
        "total_funding_pnl": float(closed["funding_pnl"].sum()) if len(closed) else 0.0,
        "total_slippage_cost": float(closed["slippage_cost"].sum()) if len(closed) else 0.0,
        "exposure_utilization": float((equity["open_positions"] > 0).mean()) if len(equity) else 0.0,
        "data_invalidations": int((trades["exit_reason"] == "DATA_INVALIDATION").sum())
        if not trades.empty else 0,
    }


def _write_breakdowns(output, trades):
    if trades.empty:
        for name in ("symbol", "direction", "month", "quarter"):
            _write_frame(output / f"{name}_performance.csv", pd.DataFrame())
        return
    closed = trades.dropna(subset=["net_pnl"]).copy()
    exit_datetime = pd.to_datetime(closed["exit_time_ms"], unit="ms", utc=True)
    closed["exit_month"] = exit_datetime.dt.strftime("%Y-%m")
    closed["exit_quarter"] = (
        exit_datetime.dt.year.astype(str) + "-Q" + exit_datetime.dt.quarter.astype(str)
    )
    for name, field in (
        ("symbol", "symbol"), ("direction", "direction"), ("month", "exit_month"), ("quarter", "exit_quarter"),
    ):
        grouped = closed.groupby(field, dropna=False).agg(
            trades=("trade_id", "count"), net_pnl=("net_pnl", "sum"),
            gross_pnl=("gross_pnl", "sum"), fees=("entry_fee", "sum"),
            funding_pnl=("funding_pnl", "sum"), slippage_cost=("slippage_cost", "sum"),
        ).reset_index()
        _write_frame(output / f"{name}_performance.csv", grouped)


def _persist(connection, run_id, config, trades, equity, metrics):
    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute(
            """INSERT INTO strategies (strategy_id,name,config_json,created_at_ms)
               VALUES (?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET config_json=excluded.config_json""",
            [config.name, config.name, json.dumps(config.__dict__, sort_keys=True), now_ms()],
        )
        connection.execute(
            """INSERT INTO portfolio_configs (config_id,name,initial_capital,allocation_mode,
               max_concurrent_positions,leverage_mode,margin_mode,trading_directions)
               VALUES (?, ?, ?, 'fixed', ?, 'custom', ?, 'LONG,SHORT')
               ON CONFLICT (name) DO UPDATE SET initial_capital=excluded.initial_capital,
               max_concurrent_positions=excluded.max_concurrent_positions""",
            [config.name, config.name, config.initial_capital, config.max_positions, config.margin_mode],
        )
        connection.execute(
            """INSERT INTO backtest_runs (backtest_run_id,strategy_id,config_id,started_at_ms,
               completed_at_ms,status,parameters_json,metrics_json)
               VALUES (?, ?, ?, ?, ?, 'COMPLETED', ?, ?)""",
            [run_id, config.name, config.name, now_ms(), now_ms(),
             json.dumps(config.__dict__, sort_keys=True), json.dumps(metrics, sort_keys=True)],
        )
        if not trades.empty:
            rows = trades.assign(
                backtest_run_id=run_id,
                trade_id=lambda frame: frame["trade_id"].map(
                    lambda value: str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{value}"))
                ),
            )
            connection.register("_backtest_trades", rows)
            try:
                columns = ", ".join(rows.columns)
                connection.execute(f"INSERT INTO trades ({columns}) SELECT {columns} FROM _backtest_trades")
            finally:
                connection.unregister("_backtest_trades")
        if not equity.empty:
            rows = equity.assign(backtest_run_id=run_id)
            connection.register("_backtest_equity", rows)
            try:
                columns = ", ".join(rows.columns)
                connection.execute(f"INSERT INTO equity_curve ({columns}) SELECT {columns} FROM _backtest_equity")
            finally:
                connection.unregister("_backtest_equity")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def execute_backtest(connection, data_root, report_dir, config, symbols=None):
    discover(connection, data_root)
    symbol_filter = ""
    parameters = ["1h"]
    if symbols:
        placeholders = ",".join("?" for _ in symbols)
        symbol_filter = f"AND candle.symbol IN ({placeholders})"
        parameters.extend(sorted(symbols))
    candle_rows = connection.execute(
        f"""SELECT candle.symbol,candle.open_time_ms,candle.open,candle.high,candle.low,candle.close
            FROM valid_market_candles AS candle
            JOIN symbol_data_coverage AS coverage
              ON coverage.symbol=candle.symbol AND coverage.timeframe=candle.timeframe
            WHERE candle.timeframe=? AND coverage.eligibility IN ('VALID','PARTIAL') {symbol_filter}
            ORDER BY candle.symbol,candle.open_time_ms""",
        parameters,
    ).fetchdf()
    segment_rows = connection.execute(
        f"""SELECT segment.symbol,segment.segment_index,segment.start_open_ms,segment.end_open_ms,
                   segment.candle_count
            FROM coverage_segments AS segment
            JOIN symbol_data_coverage AS coverage
              ON coverage.symbol=segment.symbol AND coverage.timeframe=segment.timeframe
            WHERE segment.timeframe='1h' AND coverage.eligibility IN ('VALID','PARTIAL')
            {'AND segment.symbol IN (' + ','.join('?' for _ in symbols) + ')' if symbols else ''}
            ORDER BY segment.symbol,segment.segment_index""",
        sorted(symbols) if symbols else [],
    ).fetchall()
    segments_by_symbol = {}
    for symbol, segment_index, start, end, count in segment_rows:
        segments_by_symbol.setdefault(symbol, []).append((segment_index, start, end, count))
    candles_by_symbol = {
        symbol: prepare_candles(frame, segments_by_symbol[symbol], config)
        for symbol, frame in candle_rows.groupby("symbol", sort=True)
    }
    funding_by_symbol = {
        symbol: load_funding(data_root, symbol) for symbol in candles_by_symbol
    }
    trades, equity = run_backtest(candles_by_symbol, funding_by_symbol, config)
    metrics = _metrics(trades, equity, config.initial_capital)
    run_id = str(uuid.uuid4())
    _persist(connection, run_id, config, trades, equity, metrics)
    output = Path(report_dir) / "backtests" / run_id
    _write_frame(output / "trades.csv", trades)
    _write_frame(output / "equity_curve.csv", equity)
    _write_breakdowns(output, trades)
    (output / "performance.json").write_text(
        json.dumps({"run_id": run_id, "metrics": metrics}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"run_id": run_id, "symbols": len(candles_by_symbol), "metrics": metrics, "report_dir": str(output)}
