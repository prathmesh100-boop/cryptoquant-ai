# CryptoQuant AI

Modular cryptocurrency algorithmic trading **research** platform: regime detection, rule-based multi-strategy
signals, central risk management, realistic backtesting, and paper-first execution (Delta Exchange India first).

> No profitability is claimed or implied. Nothing here is validated for live use. Capital preservation and
> statistical validation come before trade frequency.

## Status: Phase 2 of 12 complete (market data)

| Area | State |
|---|---|
| Validated settings, mode safety guards, secret redaction | Implemented, tested (Phase 1) |
| Core models, OHLCV frame validation, strategy interface | Implemented, tested (Phase 1) |
| Data-quality: gaps, staleness, look-ahead-safe resampling | Implemented, tested |
| SQLite candle store (idempotent upserts, rejects incomplete candles, restart-safe) | Implemented, tested |
| Delta public REST client (chunking, 429/timeout/5xx handling, strict parsing) | Implemented, tested with mocks; **assumptions confirmed live (19 Oct 2026)** |
| Historical downloader (resume, gap refill, REST reconciliation) | Implemented, tested with fakes |
| Replay feed (no look-ahead) and CSV source | Implemented, tested |
| Live WebSocket feed (assembler, reconnect, silence detection) | Implemented; parser matches a **real capture** of the new endpoint; WS-vs-REST value equality not yet confirmed |
| Indicators, regime, strategies, risk, backtester, exchange orders, dashboard | **Not started** |

148 automated tests pass (also confirmed on Windows / Python 3.11.9). Safety-critical behaviours were mutation-checked (deliberately broken code is caught).

## Setup

Python `>=3.11,<3.13` (developed on 3.12.3). `numpy>=2.2,<2.4` is constrained because numpy 2.5 + pandas 2.3
emitted a `Timedelta` DeprecationWarning in testing.

```bash
cd cryptoquant-ai
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                                  # never commit .env
python -m pytest                                      # 148 passed
python -m app.main --check-config
```

## Phase 2 workflows

**1. Confirm the unverified exchange assumptions (do this first, needs internet):**
```bash
python scripts/smoke_test_delta.py            # REST: product facts, grid alignment, inclusive end, time semantics
                                              # (already run once on 19 Oct 2026; all REST checks passed)
python scripts/smoke_test_delta.py --ws 150   # also listen to the candle WebSocket and compare finalized bars with REST
```
It writes nothing and sends no credentials. If any line says FAIL or "REVIEW", stop and report it.

**2. Download history into SQLite (public data, no API key):**
```bash
python scripts/download_history.py --days 30                       # configured symbols/timeframes
python scripts/download_history.py --symbols BTCUSD --timeframes 1h 4h --days 90
```
Re-running resumes after the last stored candle. Gaps get one refill attempt and are then **reported, never
interpolated**. Exit code 1 means gaps remain; 2 means a data-source error.

**3. Use the data in code:**
```python
from app.storage.candle_store import SqliteCandleStore
from app.data.quality import resample
from app.data.replay import ReplayFeed

store = SqliteCandleStore.from_url("sqlite:///data/cryptoquant.db")
hourly = store.load_frame("BTCUSD", "1h")
h4 = resample(hourly, "1h", "4h")                  # complete bars only; never uses future data
for step in ReplayFeed(h4, "4h").steps(warmup=200):  # step.window holds only bars closed by step.as_of
    ...
```

## What is verified vs. not

**Verified live against the real exchange (smoke test run 19 Oct 2026, Delta India production):**
- REST `GET /v2/history/candles` on `https://api.india.delta.exchange`: `resolution`, `symbol`, `start`, `end`
  (Unix seconds); max 2000 candles/request; rows `{time, open, high, low, close, volume}`.
- `time` **is the bar OPEN time**; request `end` **is inclusive** (the client filters/dedupes for this).
- 1h, 4h and 1d bars sit on the **UTC epoch grid**, with no gaps in the sampled ranges.
- `BTCUSD`: product id 27, perpetual futures, tick 0.5, contract value 0.001 BTC, **settled in USD**, state live.
- Rate limit 20,000 per 5 min (candles cost 3); HTTP 429 returns `X-RATE-LIMIT-RESET` in ms (documented; no 429 was
  triggered, and successful responses carried no `X-RATE-LIMIT-*` headers); a `User-Agent` header is mandatory.
- Testnet REST: `https://cdn-ind.testnet.deltaex.org` (keys are environment-specific).
- WebSocket `wss://public-socket.india.delta.exchange`: subscribe shape
  `{"type":"subscribe","payload":{"channels":[{"name":"candlestick_1m","symbols":["BTCUSD"]}]}}` is accepted
  (a `subscriptions` ack follows) and candle updates arrive in a **compact schema**, epoch **microseconds**:
  `{"type":"candlestick_1m","res":"1m","sy":"BTCUSD","cst":1792388880000000,"o":..,"h":..,"l":..,"c":..,"v":..,"ts":..}`.
  Updates for the in-progress bar arrive roughly every second with running values.
- The legacy `wss://socket.india.delta.exchange` endpoint was scheduled for removal on 31 Jul 2026 and is unsupported here.

**Still not verified:**
- That a finalized WebSocket bar equals the REST bar for the same minute (volume in particular). Run
  `python scripts/smoke_test_delta.py --ws 150`; it compares them and prints any difference. Until confirmed, treat
  WS candles as provisional and reconcile from REST (the default design).
- Behaviour on HTTP 429 and WebSocket reconnect against the live exchange (mocked/local only).
- Heartbeat / ping-pong behaviour of the public socket beyond the library's default keepalive.
- 4h/1d WebSocket channels (only `candlestick_1m` was sampled).

## Design notes

- Only **complete** candles are ever stored (`upsert_candles` requires `as_of` and refuses open bars).
- WebSocket candles are provisional; `HistoryDownloader.reconcile_recent` overwrites them with REST values.
- After any reconnect the partial bar is dropped and `feed.needs_backfill` is set; `feed.is_fresh()` must be
  true before any signal is generated.
- Retrying is allowed for idempotent public **reads**. The "never blindly retry" rule applies to order placement
  (Phase 7/8).

## Known limitations

- Reactive rate limiting only (waits on 429); no proactive request budgeting.
- Silence timeout defaults to 60 s. BTCUSD updates about once per second, so this is conservative for it; a thin
  market could still trigger a harmless reconnect.
- SQLite only; the `CandleRepository` interface is the PostgreSQL migration seam.
- `1w`/`12h` timeframes are not supported yet (week alignment on Delta is unverified).
- `Dockerfile`, `compose.yaml` and the dashboard are scheduled for later phases.

## Layout

`app/config.py`, `app/models.py`; `app/data/` (`frames`, `quality`, `source`, `delta_rest`, `downloader`, `replay`,
`live`); `app/storage/candle_store.py`; `app/strategies/base.py`; `app/monitoring/logging_setup.py`;
`scripts/` (`download_history.py`, `smoke_test_delta.py`). Other packages are empty placeholders for later phases.
