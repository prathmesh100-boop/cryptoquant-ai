"""Mocked end-to-end: exchange -> downloader -> SQLite -> resample -> replay -> strategy guards."""

from datetime import timedelta

import httpx

from app.data.delta_rest import DeltaPublicClient
from app.data.downloader import HistoryDownloader
from app.data.quality import find_gaps, resample
from app.data.replay import ReplayFeed
from app.models import Direction, Regime
from app.storage.candle_store import SqliteCandleStore
from app.strategies.base import BaseStrategy, StrategyContext
from tests.conftest import T0
from tests.test_delta_rest import Server


class Probe(BaseStrategy):
    strategy_id, eligible_regimes, min_candles = "probe", frozenset({Regime.SIDEWAYS}), 6

    def _evaluate(self, ctx):
        return self._reject(ctx, ctx.as_of, "probe never trades")


def test_full_data_pipeline():
    now = T0 + timedelta(hours=72, minutes=10)
    client = DeltaPublicClient("https://example.test", transport=httpx.MockTransport(Server()),
                               clock=lambda: now, sleep=lambda s: None)
    store = SqliteCandleStore()
    rep = HistoryDownloader(client, store, clock=lambda: now).sync("BTCUSD", "1h", T0)
    assert rep.stored_total == 72 and rep.is_contiguous

    hourly = store.load_frame("BTCUSD", "1h")
    assert find_gaps(hourly, "1h") == []
    four_h = resample(hourly, "1h", "4h")
    assert len(four_h) == 18

    steps = list(ReplayFeed(four_h, "4h").steps(warmup=6))
    assert len(steps) == 13
    for s in steps:
        ctx = StrategyContext("BTCUSD", "4h", Regime.SIDEWAYS, s.window, s.as_of)
        sig = Probe().generate(ctx)
        # at each replay step data is valid, complete and fresh, so the ONLY rejection is the strategy's own
        assert sig.direction is Direction.NONE and sig.rejection_reasons == ["probe never trades"]
