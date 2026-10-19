from datetime import timedelta

import pandas as pd
import pytest

from app.data.frames import validate_frame
from app.storage.candle_store import SqliteCandleStore, StorageError
from tests.conftest import T0, make_candles

AFTER_ALL = T0 + timedelta(days=30)


def test_roundtrip_matches_frame_convention():
    s = SqliteCandleStore()
    assert s.upsert_candles(make_candles(10), as_of=AFTER_ALL) == 10
    f = s.load_frame("BTCUSD", "1h")
    validate_frame(f, "1h")
    assert len(f) == 10 and f["close"].iloc[3] == 103 and f.index[0] == T0


def test_upsert_is_idempotent_and_updates_on_conflict():
    s = SqliteCandleStore()
    c = make_candles(3)
    s.upsert_candles(c, as_of=AFTER_ALL)
    s.upsert_candles(c, as_of=AFTER_ALL)
    assert s.count("BTCUSD", "1h") == 3
    changed = c[1].model_copy(update={"close": 101.4})
    s.upsert_candles([changed], as_of=AFTER_ALL, source="ws")
    assert s.count("BTCUSD", "1h") == 3
    assert s.load_frame("BTCUSD", "1h")["close"].iloc[1] == 101.4


def test_incomplete_candle_rejected_and_nothing_written():
    s = SqliteCandleStore()
    c = make_candles(3)
    with pytest.raises(StorageError, match="incomplete"):
        s.upsert_candles(c, as_of=T0 + timedelta(hours=2, minutes=30))  # bar 2 closes at 3:00
    assert s.count("BTCUSD", "1h") == 0


def test_range_filter_is_half_open_and_symbols_are_isolated():
    s = SqliteCandleStore()
    s.upsert_candles(make_candles(10), as_of=AFTER_ALL)
    s.upsert_candles(make_candles(5, symbol="ETHUSD"), as_of=AFTER_ALL)
    f = s.load_frame("BTCUSD", "1h", T0 + timedelta(hours=2), T0 + timedelta(hours=5))
    assert list(f["close"]) == [102, 103, 104]
    assert s.count("ETHUSD", "1h") == 5 and s.count("BTCUSD", "4h") == 0


def test_latest_open_time_and_empty_load():
    s = SqliteCandleStore()
    assert s.latest_open_time("BTCUSD", "1h") is None
    assert s.load_frame("BTCUSD", "1h").empty
    s.upsert_candles(make_candles(4), as_of=AFTER_ALL)
    assert s.latest_open_time("BTCUSD", "1h") == T0 + timedelta(hours=3)


def test_data_survives_restart(tmp_path):
    db = tmp_path / "sub" / "cq.db"
    s = SqliteCandleStore(db)
    s.upsert_candles(make_candles(6), as_of=AFTER_ALL)
    s.close()
    s2 = SqliteCandleStore(db)
    assert s2.count("BTCUSD", "1h") == 6
    pd.testing.assert_index_equal(s2.load_frame("BTCUSD", "1h").index, pd.DatetimeIndex(
        [T0 + timedelta(hours=i) for i in range(6)], name="open_time"))


def test_from_url_and_unsupported_scheme(tmp_path):
    s = SqliteCandleStore.from_url(f"sqlite:///{tmp_path}/x.db")
    assert s.count("BTCUSD", "1h") == 0
    with pytest.raises(StorageError):
        SqliteCandleStore.from_url("postgresql://u:secret@h/db")


def test_newer_schema_version_refused(tmp_path):
    import sqlite3
    db = tmp_path / "new.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version = 99")
    con.commit(); con.close()
    with pytest.raises(StorageError, match="newer"):
        SqliteCandleStore(db)


def test_sql_injection_in_symbol_is_inert():
    s = SqliteCandleStore()
    s.upsert_candles(make_candles(2), as_of=AFTER_ALL)
    assert s.count("BTCUSD'; DROP TABLE candles;--", "1h") == 0
    assert s.count("BTCUSD", "1h") == 2
