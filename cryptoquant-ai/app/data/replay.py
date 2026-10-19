"""Historical replay: step through stored candles exposing only what was knowable at each moment."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from app.data.frames import timeframe_delta, validate_frame


@dataclass(frozen=True)
class ReplayStep:
    """``as_of`` is the close time of the newest bar in ``window``; ``window`` holds completed bars only.

    ``window`` is a read-only-by-convention view; do not mutate it.
    """

    as_of: datetime
    window: pd.DataFrame


class ReplayFeed:
    def __init__(self, frame: pd.DataFrame, timeframe: str) -> None:
        validate_frame(frame, timeframe)  # sorted, unique, aligned, gap-free
        self._frame = frame
        self._timeframe = timeframe

    def steps(self, warmup: int = 0) -> Iterator[ReplayStep]:
        """Yield one step per bar close, skipping the first ``warmup`` bars (indicator warm-up)."""
        step = timeframe_delta(self._timeframe)
        for i in range(max(warmup, 1) - 1, len(self._frame)):
            window = self._frame.iloc[: i + 1]
            yield ReplayStep(as_of=window.index[-1].to_pydatetime() + step, window=window)
