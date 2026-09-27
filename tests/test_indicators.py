import numpy as np
import pandas as pd
import pytest
import ta

from bot.market import indicators as ind

# Wilder's RSI worked example (as published in StockCharts' ChartSchool RSI spreadsheet).
WILDER_CLOSES = [
    44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433,
    46.0826, 45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116,
    46.2222, 45.6439, 46.2122, 46.2521, 45.7137, 46.4515, 45.7835, 45.3548, 44.0288,
    44.1783, 44.2181, 44.5672, 43.4205, 42.6628, 43.1314,
]
WILDER_RSI = {14: 70.53, 15: 66.32, 16: 66.55, 17: 69.41, 18: 66.36, 19: 57.97}


def random_ohlcv(n=1000, seed=7):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    spread = np.abs(rng.normal(0, 0.006, n)) * close
    high = close + spread
    low = close - spread
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum.reduce([high, open_, close])
    low = np.minimum.reduce([low, open_, close])
    volume = rng.uniform(100, 1000, n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=pd.Index(np.arange(n) * 900_000, name="open_time_ms"),
    )


# --- known values ----------------------------------------------------------------------


def test_sma_and_ema_hand_computed():
    s = pd.Series([1.0, 2, 3, 4, 5, 6])
    assert ind.sma(s, 3).tolist()[2:] == [2, 3, 4, 5]
    # EMA(3): alpha = 0.5, seeded with SMA of first 3 = 2
    e = ind.ema(s, 3)
    assert np.isnan(e.iloc[1])
    assert e.iloc[2:].tolist() == [2.0, 3.0, 4.0, 5.0]


def test_rsi_matches_wilder_example():
    r = ind.rsi(pd.Series(WILDER_CLOSES), 14)
    assert r.iloc[:14].isna().all()
    for i, expected in WILDER_RSI.items():
        assert r.iloc[i] == pytest.approx(expected, abs=0.01)


def test_rsi_edge_cases():
    assert ind.rsi(pd.Series(np.arange(1.0, 30.0)), 14).iloc[-1] == 100
    assert ind.rsi(pd.Series(np.arange(30.0, 1.0, -1)), 14).iloc[-1] == 0
    assert ind.rsi(pd.Series([5.0] * 30), 14).iloc[-1] == 50


def test_atr_hand_computed():
    high = pd.Series([10.0, 11, 12, 11])
    low = pd.Series([9.0, 9, 10, 8])
    close = pd.Series([9.5, 10.5, 11, 9])
    tr = ind.true_range(high, low, close)
    assert tr.tolist() == [1.0, 2.0, 2.0, 3.0]
    # ATR(2): seed = mean(1, 2) = 1.5, then (1.5*1 + 2)/2 = 1.75, then (1.75 + 3)/2 = 2.375
    assert ind.atr(high, low, close, 2).tolist()[1:] == [1.5, 1.75, 2.375]


def test_bollinger_population_std():
    s = pd.Series([2.0, 4, 4, 4, 5, 5, 7, 9])  # mean 5, population std 2
    mid, upper, lower = ind.bollinger(s, n=8, mult=2)
    assert mid.iloc[-1] == 5
    assert upper.iloc[-1] == 9
    assert lower.iloc[-1] == 1


def test_volume_ratio():
    v = pd.Series([10.0] * 20 + [30.0])
    assert ind.volume_ratio(v, 20) == 3.0
    assert ind.volume_ratio(pd.Series([1.0] * 5), 20) is None


def test_macd_is_fast_minus_slow_ema():
    df = random_ohlcv(300)
    line, sig, hist = ind.macd(df["close"])
    expected = ind.ema(df["close"], 12) - ind.ema(df["close"], 26)
    pd.testing.assert_series_equal(line, expected)
    pd.testing.assert_series_equal(hist, line - sig)


# --- cross-check against the `ta` library (independent implementation) -----------------
# `ta` seeds its smoothing with the first value instead of an SMA, so compare only the
# tail of a long series, where the seed's influence has decayed away.

TAIL = 100


def assert_tail_close(ours, theirs, tol=1e-6):
    np.testing.assert_allclose(
        ours.iloc[-TAIL:].to_numpy(), theirs.iloc[-TAIL:].to_numpy(), rtol=tol, atol=tol
    )


def test_cross_check_rsi_ema_macd():
    close = random_ohlcv()["close"]
    assert_tail_close(ind.rsi(close), ta.momentum.RSIIndicator(close, 14).rsi())
    for n in (20, 50):
        assert_tail_close(ind.ema(close, n), ta.trend.EMAIndicator(close, n).ema_indicator())
    line, sig, hist = ind.macd(close)
    ref = ta.trend.MACD(close, 26, 12, 9)
    assert_tail_close(line, ref.macd())
    assert_tail_close(sig, ref.macd_signal())
    assert_tail_close(hist, ref.macd_diff())


def test_cross_check_bollinger_atr():
    df = random_ohlcv()
    mid, upper, lower = ind.bollinger(df["close"])
    ref = ta.volatility.BollingerBands(df["close"], 20, 2)
    assert_tail_close(mid, ref.bollinger_mavg())
    assert_tail_close(upper, ref.bollinger_hband())
    assert_tail_close(lower, ref.bollinger_lband())
    ref_atr = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], 14)
    assert_tail_close(ind.atr(df["high"], df["low"], df["close"]), ref_atr.average_true_range())


# --- levels, trend, summary ------------------------------------------------------------


def test_swing_levels_zigzag():
    # Peaks at ~110 and ~120, troughs at ~90 and ~95; price 100.
    highs = [100, 105, 110, 105, 100, 97, 100, 110, 120, 110, 100, 99, 100, 105, 110.5, 105, 100]
    lows = [h - 5 for h in highs]
    lows[5], lows[11] = 90, 95
    s_highs, s_lows = pd.Series(highs, dtype=float), pd.Series(lows, dtype=float)
    supports, resistances = ind.swing_levels(s_highs, s_lows, price=100, window=2)
    assert supports == [95, 90]
    assert resistances[0] == pytest.approx(110.25)  # 110 and 110.5 merged (within 1%)
    assert resistances[1] == 120


def test_trend_label():
    assert ind.trend_label(110, 105, 100, 95) == "up"
    assert ind.trend_label(90, 95, 100, 105) == "down"
    assert ind.trend_label(100, 105, 100, 95) == "mixed"
    assert ind.trend_label(100, 105, 100, None) is None


def test_compute_and_derive():
    df = random_ohlcv(500)
    base = ind.compute(df)
    assert base.ema200 is not None and base.rsi is not None
    price = base.last_close
    tf = ind.derive(base, price)
    assert 0 <= tf.rsi <= 100
    assert tf.trend in ("up", "down", "mixed")
    assert tf.atr_pct == pytest.approx(base.atr / price * 100, rel=1e-3)
    expected_b = (price - base.bb_lower) / (base.bb_upper - base.bb_lower)
    assert tf.bb_pct_b == pytest.approx(expected_b, abs=1e-3)


def test_compute_short_history_leaves_long_indicators_empty():
    base = ind.compute(random_ohlcv(60))
    assert base.ema200 is None
    assert base.ema50 is not None
