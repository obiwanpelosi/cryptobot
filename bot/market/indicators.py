"""Technical indicators as pure functions over pandas Series.

Formulas follow TradingView's Pine built-ins so values can be checked against charts:
- ta.rma (Wilder) and ta.ema are seeded with an SMA of the first `n` values.
- ta.rsi uses rma of gains/losses; ta.atr uses rma of true range.
- ta.stdev (used by Bollinger) is the population standard deviation.
"""

from __future__ import annotations

import math
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel

Trend = Literal["up", "down", "mixed"]
Direction = Literal["rising", "falling", "flat"]


def _seeded_ewm(series: pd.Series, n: int, alpha: float) -> pd.Series:
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if len(valid) == 0:
        return pd.Series(out, index=series.index)
    start = valid[0]
    if len(values) - start < n:
        return pd.Series(out, index=series.index)
    seed = start + n - 1
    out[seed] = values[start : start + n].mean()
    for i in range(seed + 1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return pd.Series(out, index=series.index)


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    return _seeded_ewm(series, n, 2 / (n + 1))


def rma(series: pd.Series, n: int) -> pd.Series:
    return _seeded_ewm(series, n, 1 / n)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    diff = close.diff()
    gain = rma(diff.clip(lower=0), n)
    loss = rma(-diff.clip(upper=0), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        value = 100 - 100 / (1 + gain / loss)
    value = value.where(loss != 0, 100.0)
    value = value.where(~((gain == 0) & (loss == 0)), 50.0)
    return value.where(gain.notna())


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def bollinger(
    close: pd.Series, n: int = 20, mult: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(close, n)
    std = close.rolling(n).std(ddof=0)
    return mid, mid + mult * std, mid - mult * std


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    tr = ranges.max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    return rma(true_range(high, low, close), n)


def volume_ratio(volume: pd.Series, n: int = 20) -> float | None:
    """Last volume vs the average of the `n` volumes before it."""
    if len(volume) < n + 1:
        return None
    avg = volume.iloc[-n - 1 : -1].mean()
    return float(volume.iloc[-1] / avg) if avg else None


def swing_levels(
    high: pd.Series,
    low: pd.Series,
    price: float,
    window: int = 3,
    merge_pct: float = 1.0,
    max_levels: int = 3,
) -> tuple[list[float], list[float]]:
    """Support/resistance from confirmed pivot highs/lows.

    A pivot high is a bar whose high is the max of the `window` bars either side (same for
    lows). Levels within `merge_pct` of each other are averaged. Returns
    (supports below price, nearest first; resistances above price, nearest first).
    """
    highs, lows = high.to_numpy(dtype=float), low.to_numpy(dtype=float)
    levels: list[float] = []
    for i in range(window, len(highs) - window):
        span = slice(i - window, i + window + 1)
        if highs[i] == highs[span].max():
            levels.append(highs[i])
        if lows[i] == lows[span].min():
            levels.append(lows[i])

    clusters: list[list[float]] = []
    for level in sorted(levels):
        if clusters and level <= np.mean(clusters[-1]) * (1 + merge_pct / 100):
            clusters[-1].append(level)
        else:
            clusters.append([level])
    merged = [float(np.mean(c)) for c in clusters]

    supports = sorted((lv for lv in merged if lv < price), reverse=True)[:max_levels]
    resistances = sorted(lv for lv in merged if lv > price)[:max_levels]
    return supports, resistances


# --- per-timeframe summary -------------------------------------------------------------


def _last(series: pd.Series) -> float | None:
    if series.empty:
        return None
    value = series.iloc[-1]
    return None if pd.isna(value) else float(value)


def round_sig(value: float | None, digits: int = 6) -> float | None:
    """Round to `digits` significant figures to keep the snapshot JSON compact."""
    if value is None or value == 0 or not math.isfinite(value):
        return value
    return round(value, digits - 1 - int(math.floor(math.log10(abs(value)))))


class BaseIndicators(BaseModel):
    """Values that depend only on closed candles (memoizable per candle close)."""

    last_close: float
    last_open_time_ms: int
    rsi: float | None
    ema20: float | None
    ema50: float | None
    ema200: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None
    macd_hist_dir: Direction | None
    bb_mid: float | None
    bb_upper: float | None
    bb_lower: float | None
    atr: float | None
    volume_ratio: float | None


def compute(df: pd.DataFrame) -> BaseIndicators:
    close, high, low = df["close"], df["high"], df["low"]
    line, sig, hist = macd(close)
    mid, upper, lower = bollinger(close)
    hist_dir: Direction | None = None
    if len(hist.dropna()) >= 2:
        prev, cur = hist.iloc[-2], hist.iloc[-1]
        hist_dir = "rising" if cur > prev else "falling" if cur < prev else "flat"
    return BaseIndicators(
        last_close=float(close.iloc[-1]),
        last_open_time_ms=int(df.index[-1]),
        rsi=_last(rsi(close)),
        ema20=_last(ema(close, 20)),
        ema50=_last(ema(close, 50)),
        ema200=_last(ema(close, 200)),
        macd=_last(line),
        macd_signal=_last(sig),
        macd_hist=_last(hist),
        macd_hist_dir=hist_dir,
        bb_mid=_last(mid),
        bb_upper=_last(upper),
        bb_lower=_last(lower),
        atr=_last(atr(high, low, close)),
        volume_ratio=volume_ratio(df["volume"]),
    )


class TimeframeIndicators(BaseModel):
    rsi: float | None
    ema20: float | None
    ema50: float | None
    ema200: float | None
    trend: Trend | None
    macd_hist: float | None
    macd_hist_dir: Direction | None
    bb_upper: float | None
    bb_lower: float | None
    bb_pct_b: float | None
    atr: float | None
    atr_pct: float | None
    volume_ratio: float | None


def trend_label(
    price: float, ema20: float | None, ema50: float | None, ema200: float | None
) -> Trend | None:
    if None in (ema20, ema50, ema200):
        return None
    if price > ema20 > ema50 > ema200:
        return "up"
    if price < ema20 < ema50 < ema200:
        return "down"
    return "mixed"


def derive(base: BaseIndicators, price: float) -> TimeframeIndicators:
    """Combine closed-candle indicators with the live price."""
    pct_b = None
    if base.bb_upper is not None and base.bb_lower is not None:
        width = base.bb_upper - base.bb_lower
        pct_b = (price - base.bb_lower) / width if width else None
    atr_pct = base.atr / price * 100 if base.atr is not None and price else None
    return TimeframeIndicators(
        rsi=round_sig(base.rsi, 4),
        ema20=round_sig(base.ema20),
        ema50=round_sig(base.ema50),
        ema200=round_sig(base.ema200),
        trend=trend_label(price, base.ema20, base.ema50, base.ema200),
        macd_hist=round_sig(base.macd_hist, 4),
        macd_hist_dir=base.macd_hist_dir,
        bb_upper=round_sig(base.bb_upper),
        bb_lower=round_sig(base.bb_lower),
        bb_pct_b=round_sig(pct_b, 3),
        atr=round_sig(base.atr, 4),
        atr_pct=round_sig(atr_pct, 3),
        volume_ratio=round_sig(base.volume_ratio, 3),
    )
