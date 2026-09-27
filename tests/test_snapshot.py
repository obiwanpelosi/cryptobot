import json
from decimal import Decimal as D

import numpy as np
import pytest

from bot.exchange.models import INTERVAL_MS, Candle, Tick
from bot.market.candles import CandleStore
from bot.market.context import MarketContext, Sentiment
from bot.market.snapshot import SnapshotBuilder, close_at, pct_change

TIMEFRAMES = ["15m", "1h", "4h", "1d"]
N = 500


def series(tf, end_ms, start_price, seed):
    rng = np.random.default_rng(seed)
    step = INTERVAL_MS[tf]
    closes = start_price * np.exp(np.cumsum(rng.normal(0, 0.01, N)))
    first_open = end_ms - N * step
    out = []
    for i, c in enumerate(closes):
        o = first_open + i * step
        out.append(Candle(o, c, c * 1.005, c * 0.995, c, 100.0 + i, o + step - 1))
    return out


NOW_MS = 1_800_000_000_000 - (1_800_000_000_000 % INTERVAL_MS["1d"])


def make_builder(with_ticks=True):
    store = CandleStore(["SOLUSDT", "BTCUSDT"], TIMEFRAMES, max_candles=N)
    for sym, px, seed in (("SOLUSDT", 120, 1), ("BTCUSDT", 80_000, 2)):
        for j, tf in enumerate(TIMEFRAMES):
            store.replace(sym, tf, series(tf, NOW_MS, px, seed * 10 + j))
    ctx = MarketContext(client=None, symbols=["SOLUSDT"])
    ctx.sentiment = Sentiment(fear_greed=40, classification="Fear", as_of=1)
    ticks = {}
    if with_ticks:
        ticks = {
            "SOLUSDT": Tick("SOLUSDT", D("121.5"), D("1"), NOW_MS),
            "BTCUSDT": Tick("BTCUSDT", D("81000"), D("1"), NOW_MS),
        }
    return SnapshotBuilder(store, ctx, ticks, high_lookback_hours=72), store


def test_close_at_and_pct_change():
    candles = series("1h", NOW_MS, 100, 3)
    store = CandleStore(["X"], ["1h"])
    store.replace("X", "1h", candles)
    df = store.frame("X", "1h")
    # The candle that closed exactly 24h before NOW is 24 candles from the end.
    assert close_at(df, "1h", NOW_MS - 24 * INTERVAL_MS["1h"]) == pytest.approx(candles[-25].close)
    assert close_at(df, "1h", 0) is None
    assert pct_change(110, 100) == pytest.approx(10)
    assert pct_change(110, None) is None


def test_snapshot_fields_and_compact_json():
    builder, _ = make_builder()
    snap = builder.build("SOLUSDT", now_ms=NOW_MS)
    assert snap.price == 121.5
    assert set(snap.timeframes) == set(TIMEFRAMES)
    assert snap.timeframes["1h"].ema200 is not None
    assert snap.change_1h_pct is not None and snap.change_7d_pct is not None
    assert snap.recent_high >= snap.price >= snap.recent_low
    assert snap.drop_from_high_pct >= 0
    assert snap.btc is not None and snap.btc.trend_4h in ("up", "down", "mixed")
    assert snap.sentiment.fear_greed == 40
    assert snap.derivatives is None

    raw = snap.model_dump_json()
    assert len(raw) < 4096
    data = json.loads(raw)
    assert not any(isinstance(v, list) and len(v) > 10 for v in data.values())


def test_btc_snapshot_has_no_nested_btc():
    builder, _ = make_builder()
    assert builder.build("BTCUSDT", now_ms=NOW_MS).btc is None


def test_indicators_memoized_until_new_candle():
    builder, store = make_builder()
    first = builder.base_indicators("SOLUSDT", "15m")
    assert builder.base_indicators("SOLUSDT", "15m") is first
    last = store.frame("SOLUSDT", "15m").index[-1]
    step = INTERVAL_MS["15m"]
    store.add("SOLUSDT", "15m", Candle(last + step, 1, 1, 1, 130, 1, last + 2 * step - 1))
    assert builder.base_indicators("SOLUSDT", "15m") is not first


def test_not_loaded_returns_none_and_falls_back_to_candle_price():
    builder, store = make_builder(with_ticks=False)
    snap = builder.build("SOLUSDT", now_ms=NOW_MS)
    assert snap.price == pytest.approx(float(store.frame("SOLUSDT", "15m")["close"].iloc[-1]))
    assert builder.build("LINKUSDT", now_ms=NOW_MS) is None
