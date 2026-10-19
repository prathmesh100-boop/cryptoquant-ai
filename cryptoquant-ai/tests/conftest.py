from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.config import SUPPORTED_TIMEFRAMES


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Remove CQ_* vars and run from an empty dir so a developer's real .env can't leak into tests."""
    import os

    for key in list(os.environ):
        if key.startswith("CQ_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


def make_frame(n: int = 60, timeframe: str = "1h", start: datetime | None = None, seed: int = 7) -> pd.DataFrame:
    """Deterministic synthetic OHLCV frame (seeded random walk)."""
    start = start or datetime(2024, 1, 1, tzinfo=UTC)
    step = timedelta(seconds=SUPPORTED_TIMEFRAMES[timeframe])
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    open_ = np.concatenate([[100.0], close[:-1]])
    high = np.maximum(open_, close) + 0.2
    low = np.minimum(open_, close) - 0.2
    idx = pd.DatetimeIndex([start + i * step for i in range(n)], name="open_time")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 10.0}, index=idx)
