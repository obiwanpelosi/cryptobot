import asyncio
from decimal import Decimal as D

import pytest

from bot.exchange import streams
from bot.exchange.streams import BACKOFF_MAX, BACKOFF_START, PriceStream, StreamError


def mini(symbol, close, open_, t=1):
    return {
        "stream": f"{symbol.lower()}@miniTicker",
        "data": {"e": "24hrMiniTicker", "E": t, "s": symbol, "c": close, "o": open_},
    }


class FakeSocket:
    def __init__(self, messages):
        self._messages = list(messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def recv(self):
        if not self._messages:
            await asyncio.Event().wait()  # block forever, like a quiet socket
        item = self._messages.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_handle_message_updates_tick_and_change():
    stream = PriceStream(["SOLUSDT"], socket_factory=None)
    stream.handle_message(mini("SOLUSDT", "110", "100", t=42))
    tick = stream.ticks["SOLUSDT"]
    assert tick.price == D("110")
    assert tick.change_24h_pct == D("10")
    assert tick.event_time_ms == 42


def test_error_message_raises():
    stream = PriceStream(["SOLUSDT"], socket_factory=None)
    with pytest.raises(StreamError):
        stream.handle_message({"e": "error", "type": "X", "m": "boom"})


def test_backoff_grows_caps_and_resets(monkeypatch):
    monkeypatch.setattr(streams.random, "uniform", lambda a, b: 0)
    stream = PriceStream(["SOLUSDT"], socket_factory=None)
    delays = [stream.next_backoff() for _ in range(8)]
    assert delays[:4] == [1, 2, 4, 8]
    assert delays[-1] == BACKOFF_MAX
    stream.handle_message(mini("SOLUSDT", "1", "1"))
    assert stream.next_backoff() == BACKOFF_START


def test_is_stale():
    clock = Clock()
    stream = PriceStream(["SOLUSDT"], socket_factory=None, clock=clock)
    clock.now = 100
    assert not stream.is_stale(threshold=300)
    clock.now = 301
    assert stream.is_stale(threshold=300)
    stream.handle_message(mini("SOLUSDT", "1", "1"))
    assert not stream.is_stale(threshold=300)


async def test_run_reconnects_after_error(monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(streams.asyncio, "sleep", fake_sleep)

    sockets = [
        FakeSocket([mini("SOLUSDT", "100", "100"), {"e": "error", "type": "X", "m": "drop"}]),
        FakeSocket([ConnectionError("net down")]),
        FakeSocket([mini("SOLUSDT", "120", "100")]),
    ]
    opened = []

    def factory(names):
        opened.append(names)
        return sockets[len(opened) - 1]

    stream = PriceStream(["SOLUSDT"], factory)
    task = asyncio.create_task(stream.run())
    for _ in range(50):
        await real_sleep(0)
        if stream.ticks.get("SOLUSDT") and stream.ticks["SOLUSDT"].price == D("120"):
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(opened) == 3
    assert opened[0] == ["solusdt@miniTicker"]
    assert stream.ticks["SOLUSDT"].price == D("120")
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]  # backoff grew between failures


def kline(symbol, interval, closed, t=0):
    return {
        "stream": f"{symbol.lower()}@kline_{interval}",
        "data": {
            "e": "kline",
            "s": symbol,
            "k": {"t": t, "T": t + 899_999, "s": symbol, "i": interval,
                  "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10", "x": closed},
        },
    }


def test_stream_names_include_klines():
    stream = PriceStream(["SOLUSDT"], socket_factory=None, kline_intervals=["15m", "1h"])
    assert stream.stream_names == ["solusdt@miniTicker", "solusdt@kline_15m", "solusdt@kline_1h"]


def test_only_closed_klines_emit_candles():
    got = []
    stream = PriceStream(
        ["SOLUSDT"], socket_factory=None, on_candle=lambda s, i, c: got.append((s, i, c))
    )
    stream.handle_message(kline("SOLUSDT", "15m", closed=False))
    assert got == []
    assert stream.last_message_at is not None  # still counts as a live message
    stream.handle_message(kline("SOLUSDT", "15m", closed=True, t=900_000))
    (symbol, interval, candle), = got
    assert (symbol, interval) == ("SOLUSDT", "15m")
    assert candle.open_time_ms == 900_000 and candle.close == 1.5 and candle.volume == 10


async def test_on_connect_fires_each_connection(monkeypatch):
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0)

    monkeypatch.setattr(streams.asyncio, "sleep", fast_sleep)
    sockets = iter([FakeSocket([ConnectionError("x")]), FakeSocket([])])
    connects = []
    stream = PriceStream(
        ["SOLUSDT"], lambda names: next(sockets), on_connect=lambda: connects.append(1)
    )
    task = asyncio.create_task(stream.run())
    for _ in range(20):
        await real_sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(connects) == 2
