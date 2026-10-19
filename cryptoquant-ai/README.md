# CryptoQuant AI

Modular cryptocurrency algorithmic trading **research** platform: regime detection, rule-based multi-strategy
signals, central risk management, realistic backtesting, and paper-first execution (Delta Exchange India first).

> No profitability is claimed or implied. Nothing here is validated for live use. Capital preservation and
> statistical validation come before trade frequency.

## Status: Phase 1 of 12 (setup, config, logging, models, strategy interface)

| Area | State |
|---|---|
| Validated settings, mode safety guards | Implemented, tested |
| Structured JSON logging with redaction | Implemented, tested |
| Core models (Candle, Signal, TradeDecision) | Implemented, tested |
| OHLCV frame validation (gaps, duplicates, alignment, UTC) | Implemented, tested |
| Strategy interface (regime/stale/incomplete-candle guards) | Implemented, tested |
| Market data, indicators, strategies, risk, backtester, exchange, dashboard | **Not started** |

## Requirements and compatibility

- Python `>=3.11,<3.13` (target 3.11; developed and tested on 3.12.3).
- `numpy>=2.2,<2.4`: numpy 2.5 with pandas 2.3 raised a `Timedelta` DeprecationWarning in testing.
- pandas 2.x, pydantic 2.x, pydantic-settings 2.x.

## Setup

```bash
cd cryptoquant-ai
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env               # edit as needed; never commit .env
```

## Commands

```bash
python -m pytest                   # run all tests
python -m app.main --check-config  # validate settings, print redacted summary
```

## Configuration

All settings use the `CQ_` prefix; nested groups use `__` (e.g. `CQ_RISK__KILL_SWITCH=true`). See `.env.example`.
Default mode is `PAPER`. `DEMO` requires credentials; `LIVE` additionally requires `CQ_LIVE_TRADING_CONFIRMED=true`.

## Layout

`app/` contains `config.py`, `models.py`, `data/` (frame validation), `strategies/` (interface), and empty packages
reserved for later phases: `indicators`, `intelligence`, `risk`, `execution`, `exchanges`, `backtesting`,
`analytics`, `storage`, `monitoring` (logging), `api`.

## Known limitations (Phase 1)

- Candle alignment assumes bars open on the UTC epoch grid; confirm against Delta Exchange in Phase 2/7.
- Symbols (`BTCUSD`, `ETHUSD`) are internal names; the exchange adapter will map them to real products.
- Exchange base/WebSocket URLs are intentionally unset until verified against the official docs.
- `Dockerfile`, `compose.yaml` and the dashboard are scheduled for later phases.
