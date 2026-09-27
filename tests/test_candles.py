import logging

from bot.exchange.models import INTERVAL_MS, Candle
from bot.market.candles import CandleStore

TF = "15m"
STEP = INTERVAL_MS[TF]


def candle(i, close=100.0):
    return Candle(i * STEP, close, close + 1, close - 1, close, 10.0, (i + 1) * STEP - 1)


class FakeKlines:
    def __init__(self, series):
        self.series = series  # {(symbol, tf): [Candle, ...]}
        self.calls = []

    async def get_klines(self, symbol, interval, limit=500):
        self.calls.append((symbol, interval, limit))
        return self.series[(symbol, interval)][-limit:]


def make_store(n=10, max_candles=500):
    store = CandleStore(["SOLUSDT"], [TF], max_candles=max_candles)
    store.replace("SOLUSDT", TF, [candle(i) for i in range(n)])
    return store


async def test_load_fetches_every_symbol_and_timeframe():
    series = {(s, tf): [candle(i) for i in range(5)] for s in ("A", "B") for tf in ("15m", "1h")}
    client = FakeKlines(series)
    store = CandleStore(["A", "B"], ["15m", "1h"], max_candles=500)
    await store.load(client)
    assert len(client.calls) == 4
    assert store.is_loaded("A") and store.is_loaded("B")
    assert len(store.frame("A", "1h")) == 5


def test_add_appends_ignores_duplicates_and_bumps_version():
    store = make_store(3)
    v = store.version("SOLUSDT", TF)
    assert store.add("SOLUSDT", TF, candle(3, close=105)) is False
    assert store.frame("SOLUSDT", TF)["close"].iloc[-1] == 105
    assert store.version("SOLUSDT", TF) == v + 1
    store.add("SOLUSDT", TF, candle(3, close=999))  # duplicate open time
    store.add("SOLUSDT", TF, candle(1, close=999))  # older
    assert len(store.frame("SOLUSDT", TF)) == 4
    assert store.frame("SOLUSDT", TF)["close"].iloc[-1] == 105
    assert store.version("SOLUSDT", TF) == v + 1


def test_add_detects_gap(caplog):
    store = make_store(3)
    with caplog.at_level(logging.WARNING):
        assert store.add("SOLUSDT", TF, candle(5)) is True
    assert "gap" in caplog.text


def test_history_cap():
    store = make_store(5, max_candles=5)
    store.add("SOLUSDT", TF, candle(5))
    frame = store.frame("SOLUSDT", TF)
    assert len(frame) == 5
    assert frame.index[0] == 1 * STEP


async def test_backfill_merges_missing_candles():
    store = make_store(10)
    client = FakeKlines({("SOLUSDT", TF): [candle(i) for i in range(15)]})
    await store.backfill(client)
    frame = store.frame("SOLUSDT", TF)
    assert list(frame.index) == [i * STEP for i in range(15)]
    assert client.calls == [("SOLUSDT", TF, 100)]


async def test_backfill_full_reload_when_gap_too_large():
    store = make_store(10)
    # REST's latest 100 candles start well after our last candle (index 9): gap too large.
    client = FakeKlines({("SOLUSDT", TF): [candle(i) for i in range(300, 700)]})
    await store.backfill(client)
    frame = store.frame("SOLUSDT", TF)
    assert client.calls[-1] == ("SOLUSDT", TF, 500)
    assert len(frame) == 400 and frame.index[-1] == 699 * STEP


async def test_backfill_failure_keeps_existing(caplog):
    class Failing:
        async def get_klines(self, *a, **k):
            raise ConnectionError("down")

    store = make_store(10)
    with caplog.at_level(logging.WARNING):
        await store.backfill(Failing())
    assert len(store.frame("SOLUSDT", TF)) == 10
    assert "Backfill failed" in caplog.text
