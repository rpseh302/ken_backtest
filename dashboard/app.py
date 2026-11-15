from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.data import (add_chart_indicators, discover_market_files, read_candles,
                             read_run_frames, read_runs)

st.set_page_config(page_title="Ken Backtest | Quant Terminal", page_icon="📈", layout="wide")
DATA_ROOT = Path(os.environ.get("KEN_BACKTEST_DATA_ROOT", "data"))
DB_PATH = Path(os.environ.get("KEN_BACKTEST_DB_PATH", str(DATA_ROOT / "backtester.duckdb")))

st.markdown("""<style>
.stApp {background:#0B1220;color:#E5E7EB}
[data-testid="stSidebar"] {background:#0F172A}
[data-testid="stMetric"] {background:#111827;border:1px solid #243244;padding:14px;border-radius:10px}
[data-testid="stMetricLabel"] {color:#9CA3AF}
[data-testid="stMetricValue"] {color:#E5E7EB}
[data-testid="stDataFrame"] {border:1px solid #243244;border-radius:8px}
.block-container {padding-top:1.4rem}
</style>""", unsafe_allow_html=True)


def _fmt(value, suffix="", pct=False):
    if value is None or pd.isna(value):
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:.2%}" if pct else f"{number:,.2f}{suffix}"


def _run_label(row):
    started = row.get("started_at_ms")
    date = pd.to_datetime(started, unit="ms", utc=True).strftime("%Y-%m-%d %H:%M UTC") if pd.notna(started) else str(row["run_id"])[0:8]
    return f"{date} · {str(row.get('strategy_id') or 'Backtest')} · {str(row['run_id'])[:8]}"


def _equity_chart(equity):
    if equity.empty or "timestamp_ms" not in equity or "equity" not in equity:
        st.info("선택한 Run에 Equity Curve 데이터가 없습니다.")
        return
    frame = equity.copy()
    frame["time"] = pd.to_datetime(frame["timestamp_ms"], unit="ms", utc=True)
    fig = go.Figure(go.Scatter(x=frame["time"], y=frame["equity"], mode="lines", name="Equity", line=dict(color="#22C55E", width=2)))
    fig.update_layout(template="plotly_dark", paper_bgcolor="#111827", plot_bgcolor="#0F172A", height=330, margin=dict(l=10,r=10,t=20,b=10), xaxis_title=None, yaxis_title="USDT")
    st.plotly_chart(fig, use_container_width=True)


def _overview(runs, selected_run, trades, equity):
    st.title("Overview")
    st.caption("Portfolio performance · Selected backtest run")
    metrics = selected_run.get("metrics", {}) if isinstance(selected_run.get("metrics"), dict) else {}
    cols = st.columns(4)
    items = [("Total Return", _fmt(metrics.get("total_return"), pct=True)),
             ("Final Equity", _fmt(metrics.get("final_equity"), " USDT")),
             ("Max Drawdown", _fmt(metrics.get("maximum_drawdown"), pct=True)),
             ("Sharpe Ratio", _fmt(metrics.get("sharpe_ratio"))),
             ("Profit Factor", _fmt(metrics.get("profit_factor"))),
             ("Win Rate", _fmt(metrics.get("win_rate"), pct=True)),
             ("Total Trades", str(metrics.get("total_trades", len(trades)))),
             ("Annualized Return", _fmt(metrics.get("annualized_return"), pct=True))]
    for col, (label, value) in zip([*cols, *st.columns(4)], items):
        col.metric(label, value)
    left, right = st.columns([2, 1])
    with left:
        st.subheader("Equity Curve")
        _equity_chart(equity)
    with right:
        st.subheader("Long / Short PnL")
        direction = trades.dropna(subset=["net_pnl"]).groupby("direction", as_index=False)["net_pnl"].sum() if not trades.empty and {"direction", "net_pnl"}.issubset(trades.columns) else pd.DataFrame()
        if not direction.empty:
            fig = go.Figure(go.Bar(x=direction["direction"], y=direction["net_pnl"], marker_color=["#22C55E" if x == "LONG" else "#EF4444" for x in direction["direction"]]))
            fig.update_layout(template="plotly_dark", paper_bgcolor="#111827", plot_bgcolor="#0F172A", height=330, margin=dict(l=10,r=10,t=20,b=10), yaxis_title="Net PnL (USDT)")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("거래 데이터가 없습니다.")
    st.subheader("Drawdown")
    if not equity.empty and "drawdown" in equity and "timestamp_ms" in equity:
        dd = equity.copy()
        dd["time"] = pd.to_datetime(dd["timestamp_ms"], unit="ms", utc=True)
        fig = go.Figure(go.Scatter(x=dd["time"], y=dd["drawdown"], fill="tozeroy", name="Drawdown", line=dict(color="#EF4444", width=1.5)))
        fig.update_layout(template="plotly_dark", paper_bgcolor="#111827", plot_bgcolor="#0F172A", height=220, margin=dict(l=10,r=10,t=10,b=10), yaxis_tickformat=".1%")
        st.plotly_chart(fig, use_container_width=True)
    if not trades.empty and {"net_pnl", "exit_time_ms"}.issubset(trades.columns):
        closed = trades.dropna(subset=["net_pnl", "exit_time_ms"]).copy()
        if not closed.empty:
            closed["month"] = pd.to_datetime(closed["exit_time_ms"], unit="ms", utc=True).dt.strftime("%Y-%m")
            monthly = closed.groupby("month", as_index=False)["net_pnl"].sum()
            by_symbol = closed.groupby("symbol", as_index=False)["net_pnl"].sum().sort_values("net_pnl") if "symbol" in closed else pd.DataFrame()
            left, right = st.columns(2)
            with left:
                st.subheader("Monthly Net PnL")
                fig = go.Figure(go.Bar(x=monthly["month"], y=monthly["net_pnl"], marker_color=["#22C55E" if x >= 0 else "#EF4444" for x in monthly["net_pnl"]]))
                fig.update_layout(template="plotly_dark", paper_bgcolor="#111827", plot_bgcolor="#0F172A", height=300, margin=dict(l=10,r=10,t=10,b=10), yaxis_title="USDT")
                st.plotly_chart(fig, use_container_width=True)
            with right:
                st.subheader("Symbol Contribution")
                if not by_symbol.empty:
                    fig = go.Figure(go.Bar(x=by_symbol["net_pnl"], y=by_symbol["symbol"], orientation="h", marker_color=["#22C55E" if x >= 0 else "#EF4444" for x in by_symbol["net_pnl"]]))
                    fig.update_layout(template="plotly_dark", paper_bgcolor="#111827", plot_bgcolor="#0F172A", height=300, margin=dict(l=10,r=10,t=10,b=10), xaxis_title="Net PnL (USDT)")
                    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Trading Cost & PnL")
    cost_cols = st.columns(4)
    for col, label, key in zip(cost_cols, ["Gross PnL", "Fees", "Funding PnL", "Slippage Cost"], ["gross_pnl", "total_fees", "total_funding_pnl", "total_slippage_cost"]):
        col.metric(label, _fmt(metrics.get(key), " USDT"))
    st.subheader("Trade Summary")
    if trades.empty:
        st.info("이 Run에는 저장된 거래가 없습니다.")
    else:
        display = trades.copy()
        for col in ("entry_time_ms", "exit_time_ms"):
            if col in display:
                display[col.replace("_ms", "_utc")] = pd.to_datetime(display[col], unit="ms", utc=True, errors="coerce")
        preferred = [c for c in ["symbol", "direction", "entry_time_utc", "exit_time_utc", "entry_price", "exit_price", "net_pnl", "exit_reason"] if c in display]
        st.dataframe(display[preferred], use_container_width=True, hide_index=True)


def _chart_analysis(symbols, files, run_id, trades):
    st.title("Chart Analysis")
    st.caption("OHLCV · EMA 20 / EMA 50 · ATR stop · Executed trades")
    controls = st.columns([1, 1, 1])
    symbol = controls[0].selectbox("Symbol", symbols, index=0)
    intervals = sorted(files[symbol], key=lambda x: ["1m", "5m", "15m", "1h", "4h"].index(x))
    timeframe = controls[1].selectbox("Timeframe", intervals, index=intervals.index("1h") if "1h" in intervals else 0)
    overlay = controls[2].checkbox("EMA overlays", value=True)
    try:
        candles = read_candles(files[symbol][timeframe])
        if overlay and timeframe == "1h":
            candles = add_chart_indicators(candles)
    except Exception as exc:
        st.error(f"캔들 파일을 읽을 수 없습니다: {exc}")
        return
    if candles.empty:
        st.warning("선택한 데이터셋에 캔들이 없습니다.")
        return
    first_day, last_day = candles["datetime"].min().date(), candles["datetime"].max().date()
    selected_days = st.date_input("Date range (UTC)", value=(first_day, last_day), min_value=first_day, max_value=last_day)
    if isinstance(selected_days, tuple) and len(selected_days) == 2:
        start_day, end_day = selected_days
        candles = candles[(candles["datetime"].dt.date >= start_day) & (candles["datetime"].dt.date <= end_day)].copy()
    if candles.empty:
        st.warning("선택한 기간에 캔들이 없습니다.")
        return
    max_bars = st.slider("표시할 최근 봉 수", min_value=1, max_value=min(5000, len(candles)), value=min(500, len(candles)), step=1)
    candles = candles.tail(max_bars).copy()
    fig = go.Figure(go.Candlestick(x=candles["datetime"], open=candles["open"], high=candles["high"], low=candles["low"], close=candles["close"], name=symbol))
    if overlay and timeframe == "1h":
        fig.add_trace(go.Scatter(x=candles["datetime"], y=candles["ema_fast"], name="EMA 20", line=dict(color="#FBBF24", width=1.4)))
        fig.add_trace(go.Scatter(x=candles["datetime"], y=candles["ema_slow"], name="EMA 50", line=dict(color="#60A5FA", width=1.4)))
    symbol_trades = trades[trades["symbol"] == symbol].copy() if not trades.empty and "symbol" in trades else pd.DataFrame()
    if not symbol_trades.empty:
        for _, trade in symbol_trades.iterrows():
            entry_time = pd.to_datetime(trade.get("entry_time_ms"), unit="ms", utc=True, errors="coerce")
            exit_time = pd.to_datetime(trade.get("exit_time_ms"), unit="ms", utc=True, errors="coerce")
            if pd.notna(entry_time) and candles["datetime"].min() <= entry_time <= candles["datetime"].max():
                fig.add_trace(go.Scatter(x=[entry_time], y=[trade.get("entry_price")], mode="markers", name=f"{trade.get('direction')} Entry", marker=dict(symbol="triangle-up" if trade.get("direction")=="LONG" else "triangle-down", size=11, color="#22C55E" if trade.get("direction")=="LONG" else "#EF4444"), showlegend=False))
            if pd.notna(exit_time) and pd.notna(trade.get("exit_price")) and candles["datetime"].min() <= exit_time <= candles["datetime"].max():
                fig.add_trace(go.Scatter(x=[exit_time], y=[trade.get("exit_price")], mode="markers", name="Exit", marker=dict(symbol="x", size=10, color="#E5E7EB"), showlegend=False))
            stop = trade.get("initial_stop_price")
            if pd.notna(stop) and pd.notna(entry_time) and pd.notna(exit_time) and exit_time >= candles["datetime"].min() and entry_time <= candles["datetime"].max():
                fig.add_trace(go.Scatter(x=[max(entry_time, candles["datetime"].min()), min(exit_time, candles["datetime"].max())], y=[stop, stop], mode="lines", name="Initial ATR Stop", line=dict(color="#F97316", dash="dot", width=1), showlegend=False))
    fig.update_layout(template="plotly_dark", paper_bgcolor="#111827", plot_bgcolor="#0F172A", height=650, margin=dict(l=10,r=10,t=25,b=10), xaxis_rangeslider_visible=False, legend=dict(orientation="h"))
    st.plotly_chart(fig, use_container_width=True)
    if symbol == "ABBVUSDT":
        st.caption("ABBVUSDT는 무기한계약이 아니므로 펀딩비가 적용되지 않습니다.")
    if not symbol_trades.empty:
        st.subheader("Selected Symbol Trades")
        view = symbol_trades.copy()
        for col in ("entry_time_ms", "exit_time_ms"):
            if col in view:
                view[col.replace("_ms", "_utc")] = pd.to_datetime(view[col], unit="ms", utc=True, errors="coerce")
        cols = [c for c in ["direction", "entry_time_utc", "exit_time_utc", "entry_price", "exit_price", "initial_stop_price", "gross_pnl", "net_pnl", "funding_pnl", "exit_reason"] if c in view]
        st.dataframe(view[cols], use_container_width=True, hide_index=True)
    else:
        st.info("선택한 Run에서 이 심볼의 거래 기록이 없습니다. 캔들 차트는 계속 조회할 수 있습니다.")


st.sidebar.title("KEN BACKTEST")
page = st.sidebar.radio("Workspace", ["Overview", "Chart Analysis"], label_visibility="collapsed")
files = discover_market_files(DATA_ROOT)
runs = read_runs(DATA_ROOT, DB_PATH)
if not files:
    st.error(f"Parquet 데이터를 찾지 못했습니다: {DATA_ROOT / 'parquet'}")
    st.stop()
if runs.empty:
    st.sidebar.info("완료된 Backtest Run이 없습니다.")
    if page == "Overview":
        st.title("Overview")
        st.info("백테스트 결과가 아직 없습니다. 로컬에서 백테스트를 실행하면 KPI와 거래 분석이 표시됩니다.")
        st.metric("Available Symbols", len(files))
        st.write("사용 가능한 심볼: " + ", ".join(sorted(files)))
    else:
        _chart_analysis(sorted(files), files, None, pd.DataFrame())
    st.stop()
runs = runs.copy()
runs["label"] = runs.apply(_run_label, axis=1)
selected_label = st.sidebar.selectbox("Backtest Run", runs["label"].tolist())
selected_run = runs.loc[runs["label"] == selected_label].iloc[0]
run_id = str(selected_run["run_id"])
trades, equity = read_run_frames(DATA_ROOT, run_id, DB_PATH)
if page == "Overview":
    _overview(runs, selected_run, trades, equity)
else:
    _chart_analysis(sorted(files), files, run_id, trades)
