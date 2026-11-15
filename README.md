# Binance USDT-M 선물 OHLCV 수집

현재 거래 중인 USDⓈ-M 선물 중 호가 자산이 USDT인 모든 심볼을 대상으로
2026-10-01부터 실행일의 **이틀 전(UTC)까지 포함**하여
`1m`, `5m`, `15m`, `1h`, `4h` 캔들을 수집합니다. 무기한 및 분기 계약을
포함하며, 현재 거래 중이 아닌 과거 상장폐지 심볼은 제외합니다.
각 심볼의 상장일 이전 데이터는 요청하지 않습니다.

## 설치

Python 3.11 기준:

Windows PowerShell:

```powershell
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Windows 명령 프롬프트:

```cmd
py -3.11 -m venv venv
venv\Scripts\activate.bat
python -m pip install -r requirements.txt
```

macOS/Linux:

```bash
python3.11 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
```

## 실행

```text
python -X utf8 collect_futures.py
```

테스트용으로 심볼과 기간을 제한할 수 있습니다.

```text
python -X utf8 collect_futures.py --symbols BTCUSDT --start-date 2026-10-01 --end-date 2026-11-02
```

`--intervals 1m 1h`, `--workers 4`, `--output 경로`도 지정할 수 있습니다.
`--end-date`는 UTC 기준 실행일의 이틀 전보다 늦을 수 없습니다.
전체 수집은 대량의 요청과 디스크 공간이 필요하며 오래 걸립니다.
같은 출력 경로로 여러 수집기를 동시에 실행하지 마세요.

## 결과 및 주의사항

- `data\raw\futures\um\...`: ZIP의 SHA-256 체크섬을 검증한 후 추출한 원본 CSV
- `data\parquet\<심볼>\<주기>.parquet`: 정렬된 캔들 데이터. 시간 열은 UTC datetime이며 중복 시작 시간은 오류로 처리. 저장 전 다시 읽어 원본 DataFrame과 비교
- `data\report.json`: 완료 시 심볼·주기별 행 수와 일별 미공개 파일, 처리 오류 기록

월 전체가 요청 범위에 포함되면 월별 아카이브를 우선 사용하고, 없으면 일별
파일을 시도합니다. 일부 파일이 없으면 수집된 부분만 저장하고 보고서에
누락 날짜를 기록합니다. 누락이나 오류가 있으면 종료 코드가 1입니다.
보고서는 실행이 끝나야 갱신되므로 실행 중에는 이전 보고서를 완료 결과로
해석하면 안 됩니다.

재실행 시 이미 추출된 원본 CSV는 다시 다운로드하지 않습니다. 바이낸스가
아카이브를 수정한 경우 새로 받으려면 해당 원본 CSV를 제거하고 재실행하세요.
파일의 존재와 체크섬 확인은 거래 중단, 거래소의 데이터 공백 또는 전체
캔들의 완전성을 보장하지 않습니다.

`binance-historical-data`는 아카이브 파일명과 로컬 경로 생성에 사용합니다.
패키지의 기본 다운로드는 오류와 누락을 명확히 구분하지 못하고 월별 파일이
없는 경우 일별 대체를 보장하지 않으므로, 다운로드와 무결성 검사 및 폴백은
이 스크립트에서 수행합니다.

검증: `python -m unittest -v test_collect_futures`

## Phase 1 — 데이터 인벤토리 및 품질 검증

수집된 Parquet는 변경하지 않는 원본으로 취급합니다. DuckDB 카탈로그는
`data\backtester.duckdb`에 생성되며, Python에서는 전체 데이터셋을 Pandas로
한꺼번에 읽지 않습니다. PyArrow로 제한된 행 배치로 파일을 읽고 DuckDB의
벡터화 SQL로 봉별 검사를 수행합니다. `--batch-size`와 `--memory-limit`로
배치 크기 및 DuckDB 메모리 한도를 조정할 수 있습니다.

```powershell
python -m pip install -r requirements.txt
python -m backtester data discover
python -m backtester data inventory
python -m backtester data validate
python -m backtester data validate --timeframe 1m
python -m backtester data report
python -m backtester data status
python -m backtester data resume
```

공통 옵션은 `--data-root`, `--db`, `--report-dir`, `--memory-limit`,
`--threads`입니다. 검증 옵션은 `--batch-size`, `--abs-tolerance`,
`--rel-tolerance`, `--timeframe`, `--force`입니다. 같은 원본의 일반 검증은
멱등적으로 완료된 결과를 재사용하며, `--force`는 새 검증 실행을 명시적으로
시작합니다. 중단된 실행은 `resume` 명령으로 이어서 처리합니다.

시간은 UTC 기준 epoch milliseconds로 카탈로그에 저장됩니다. 입력의
`count`/`trade_count` 및 `taker_buy_volume`/`taker_buy_base_volume` 열은
동일한 의미의 별칭으로 정규화됩니다. `market_candles`는 원본 뷰,
`valid_market_candles`는 검증을 통과한 봉의 조회 뷰입니다. 유효 뷰는
완전한 동일 중복을 한 건으로만 노출하고, 충돌 중복과 다른 품질 오류가 있는
행은 제외합니다. 원본 파일은 보존되고 각 문제는 DuckDB의
`data_quality_issues`에 원본 경로·행 번호·봉 시각·사유와 함께 남습니다.

`data\reports\` 아래에 discovery, inventory, 품질, 심볼 커버리지,
성능 보고서가 생성됩니다. 내부 공백만 누락 봉으로 계산하며 최초·최종 관측
시각 밖은 결손으로 보지 않습니다. 끊기지 않은 짧은 이력도 데이터 품질이
정상이면 `VALID`일 수 있고, 전략별 warm-up 적격성과는 분리됩니다.
거래소의 현재 심볼 상태로 과거 상장 여부를 추론하지 않습니다.

Phase 1은 데이터 엔진만 포함하며 매매·백테스트 로직은 구현하지 않습니다.
향후 포트폴리오 기본값 스키마는 초기 자본 10,000 USDT, 동일 배분,
최대 동시 포지션 10, 사용자 지정 레버리지, 격리 마진, LONG/SHORT 양방향을
기록합니다. 동일 배분은 사용 가능한 포트폴리오 슬롯에 마진 예산을 균등
배분한다는 의미입니다.

자동화 테스트:

```powershell
python -m unittest -v
```

## EMA 20/50 + ATR 백테스트

`ema_20_50_atr`는 검증된 1시간 봉과 커버리지 세그먼트만 사용합니다. EMA 20/50과
Wilder ATR 14는 각 연속 세그먼트에서 독립적으로 초기화되며, 신호는 봉 마감 후
계산하고 다음 봉 시가에 체결합니다. 초기 ATR 스톱, 불리한 슬리피지, taker 수수료,
펀딩비 및 포트폴리오 한도를 적용합니다.

기본 설정은 [`config/ema_20_50_atr.json`](config/ema_20_50_atr.json)에 있으며,
수수료·슬리피지·자본·배분·레버리지 등은 여기서 변경합니다.

```powershell
python -m backtester backtest --strategy ema_20_50_atr `
  --symbols BTCUSDT ETHUSDT ABBVUSDT FRHCUSDT
```

결과는 `data\reports\backtests\<run-id>\` 아래의 `trades.csv`,
`equity_curve.csv`, 심볼·방향·월·분기별 CSV 및 `performance.json`에 저장됩니다.
동시에 DuckDB의 `backtest_runs`, `trades`, `equity_curve`에도 기록됩니다.
세그먼트 경계에서 열린 포지션은 가격을 만들지 않고 `DATA_INVALIDATION`으로
명시 종료합니다. 자세한 이벤트 순서는
[`docs/ema_20_50_atr_architecture.md`](docs/ema_20_50_atr_architecture.md)를 참고하세요.

## USDⓈ-M 무기한 계약 펀딩비 수집

OHLCV 캔들에는 펀딩비가 포함되지 않습니다. 이미 내려받은 Parquet의 심볼별
실제 최초 `open_time`부터 최종 `close_time`까지를 읽어, Binance USDⓈ-M
Futures API의 `/fapi/v1/fundingRate`에서 펀딩비를 별도로 수집할 수 있습니다.
이 수집은 캔들 파일을 수정하거나 병합하지 않습니다.

```powershell
python -X utf8 collect_funding_rates.py --data-root data --workers 4
```

수집 대상 중 `PERPETUAL` 계약만 API로 조회합니다. 분기·납기 계약 등
비무기한 계약은 펀딩비가 없으므로 `data\funding_rate_report.json`의
`skipped_non_perpetual_symbols`에 기록합니다. 심볼별 원본 응답은
`data\raw\futures\um\funding_rate\<심볼>.json`, 정규화된 별도 Parquet는
`data\parquet\<심볼>\funding_rate.parquet`에 저장됩니다. 출력 Parquet에는
`symbol`, `funding_time`, `funding_time_ms`, `funding_rate`, `mark_price`,
`rate_type`, `source_url`이 포함됩니다.
