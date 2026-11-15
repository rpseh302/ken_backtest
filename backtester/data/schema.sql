CREATE TABLE IF NOT EXISTS symbols (
    symbol VARCHAR PRIMARY KEY,
    exchange VARCHAR NOT NULL DEFAULT 'BINANCE',
    market_type VARCHAR NOT NULL DEFAULT 'USDT_M',
    exchange_status VARCHAR NOT NULL DEFAULT 'UNKNOWN',
    first_observed_ms BIGINT,
    last_observed_ms BIGINT,
    updated_at_ms BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS datasets (
    dataset_id VARCHAR PRIMARY KEY,
    symbol VARCHAR NOT NULL,
    timeframe VARCHAR NOT NULL,
    source_path VARCHAR NOT NULL UNIQUE,
    source_sha256 VARCHAR NOT NULL,
    source_size_bytes BIGINT NOT NULL,
    source_mtime_ns BIGINT NOT NULL,
    source_rows BIGINT NOT NULL,
    schema_json VARCHAR NOT NULL,
    discovered_at_ms BIGINT NOT NULL,
    validation_status VARCHAR NOT NULL DEFAULT 'PENDING'
        CHECK (validation_status IN ('PENDING','VALIDATED','INVALID','ERROR')),
    last_validation_run_id VARCHAR
);

CREATE TABLE IF NOT EXISTS symbol_data_coverage (
    symbol VARCHAR NOT NULL,
    timeframe VARCHAR NOT NULL,
    first_open_ms BIGINT,
    last_open_ms BIGINT,
    expected_candles BIGINT NOT NULL DEFAULT 0,
    observed_candles BIGINT NOT NULL DEFAULT 0,
    missing_candles BIGINT NOT NULL DEFAULT 0,
    duplicate_candles BIGINT NOT NULL DEFAULT 0,
    invalid_candles BIGINT NOT NULL DEFAULT 0,
    incomplete_candles BIGINT NOT NULL DEFAULT 0,
    coverage_ratio DOUBLE,
    eligibility VARCHAR NOT NULL CHECK (eligibility IN ('VALID','PARTIAL','INVALID','MISSING')),
    updated_at_ms BIGINT NOT NULL,
    PRIMARY KEY (symbol, timeframe)
);

CREATE TABLE IF NOT EXISTS coverage_segments (
    symbol VARCHAR NOT NULL,
    timeframe VARCHAR NOT NULL,
    segment_index INTEGER NOT NULL,
    start_open_ms BIGINT NOT NULL,
    end_open_ms BIGINT NOT NULL,
    candle_count BIGINT NOT NULL,
    PRIMARY KEY (symbol, timeframe, segment_index)
);

CREATE TABLE IF NOT EXISTS data_quality_runs (
    run_id VARCHAR PRIMARY KEY,
    started_at_ms BIGINT NOT NULL,
    completed_at_ms BIGINT,
    status VARCHAR NOT NULL,
    data_root VARCHAR NOT NULL,
    timeframe_filter VARCHAR,
    batch_size INTEGER NOT NULL,
    files_total BIGINT NOT NULL DEFAULT 0,
    files_processed BIGINT NOT NULL DEFAULT 0,
    rows_processed BIGINT NOT NULL DEFAULT 0,
    peak_memory_bytes BIGINT,
    error_message VARCHAR
);

CREATE TABLE IF NOT EXISTS data_quality_issues (
    issue_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    dataset_id VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    timeframe VARCHAR NOT NULL,
    source_path VARCHAR NOT NULL,
    source_row_number BIGINT,
    open_time_ms BIGINT,
    issue_code VARCHAR NOT NULL,
    details VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_configs (
    config_id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL UNIQUE,
    initial_capital DOUBLE NOT NULL DEFAULT 10000,
    allocation_mode VARCHAR NOT NULL DEFAULT 'equal',
    max_concurrent_positions INTEGER NOT NULL DEFAULT 10,
    leverage_mode VARCHAR NOT NULL DEFAULT 'custom',
    margin_mode VARCHAR NOT NULL DEFAULT 'isolated',
    trading_directions VARCHAR NOT NULL DEFAULT 'LONG,SHORT'
);

CREATE TABLE IF NOT EXISTS strategies (
    strategy_id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL UNIQUE,
    config_json VARCHAR NOT NULL DEFAULT '{}',
    created_at_ms BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    backtest_run_id VARCHAR PRIMARY KEY,
    strategy_id VARCHAR,
    config_id VARCHAR,
    started_at_ms BIGINT NOT NULL,
    completed_at_ms BIGINT,
    status VARCHAR NOT NULL,
    parameters_json VARCHAR NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id VARCHAR PRIMARY KEY,
    backtest_run_id VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    direction VARCHAR NOT NULL,
    entry_time_ms BIGINT NOT NULL,
    exit_time_ms BIGINT,
    entry_price DOUBLE NOT NULL,
    exit_price DOUBLE,
    quantity DOUBLE NOT NULL,
    pnl DOUBLE
);

CREATE TABLE IF NOT EXISTS equity_curve (
    backtest_run_id VARCHAR NOT NULL,
    timestamp_ms BIGINT NOT NULL,
    equity DOUBLE NOT NULL,
    PRIMARY KEY (backtest_run_id, timestamp_ms)
);
