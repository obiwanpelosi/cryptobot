"""Structured, compact market snapshot per symbol (requirements.md §5.4).

This is what the dip rules evaluate (Phase 4) and what the AI receives (Phase 6).
Summarised values only: no candle arrays.
"""

from __future__ import annotations

import time
from collections.abc import Mapping

import pandas as pd
from pydantic import BaseModel

from bot.exchange.models import INTERVAL_MS, Tick
from bot.market import indicators as ind
from bot.market.candles import CandleStore
from bot.market.context import Derivatives, MarketContext, Sentiment
from bot.settings import MacroEvent

HOUR_MS = 3_600_000
LEVELS_TIMEFRAME = "4h"
LEVELS_LOOKBACK_CANDLES = 120  # 20 days of 4h candles


class BtcState(BaseModel):
    price: float
    change_1h_pct: float | None
    change_24h_pct: float | None
    trend_1h: ind.Trend | None
    trend_4h: ind.Trend | None
    trend_1d: ind.Trend | None


class MarketSnapshot(BaseModel):
    symbol: str
    price: float
    as_of: int  # epoch seconds
    change_1h_pct: float | None
    change_24h_pct: float | None
    change_7d_pct: float | None
    high_lookback_hours: int
    recent_high: float | None
    recent_low: float | None
    drop_from_high_pct: float | None
    rise_from_low_pct: float | None
    timeframes: dict[str, ind.TimeframeIndicators]
    supports: list[float]
    resistances: list[float]
    btc: BtcState | None
    derivatives: Derivatives | None
    sentiment: Sentiment | None
    macro: list[MacroEvent]


def close_at(df: pd.DataFrame | None, tf: str, ts_ms: int) -> float | None:
    """Close of the last candle that had closed by `ts_ms`."""
    if df is None or df.empty:
        return None
    closed = df[df.index + INTERVAL_MS[tf] <= ts_ms]
    return None if closed.empty else float(closed["close"].iloc[-1])


def pct_change(now: float, then: float | None) -> float | None:
    return None if not then else (now - then) / then * 100


class SnapshotBuilder:
    def __init__(
        self,
        store: CandleStore,
        context: MarketContext,
        ticks: Mapping[str, Tick],
        *,
        high_lookback_hours: int = 72,
        btc_symbol: str | None = "BTCUSDT",
    ):
        self.store = store
        self.context = context
        self.ticks = ticks
        self.high_lookback_hours = high_lookback_hours
        self.btc_symbol = btc_symbol
        self._cache: dict[tuple[str, str], tuple[int, ind.BaseIndicators]] = {}

    def base_indicators(self, symbol: str, tf: str) -> ind.BaseIndicators | None:
        """Closed-candle indicators, recomputed only when a new candle closes."""
        df = self.store.frame(symbol, tf)
        if df is None or df.empty:
            return None
        version = self.store.version(symbol, tf)
        cached = self._cache.get((symbol, tf))
        if cached and cached[0] == version:
            return cached[1]
        base = ind.compute(df)
        self._cache[(symbol, tf)] = (version, base)
        return base

    def price(self, symbol: str) -> float | None:
        tick = self.ticks.get(symbol)
        if tick is not None:
            return float(tick.price)
        df = self.store.frame(symbol, self.store.timeframes[0])
        return None if df is None or df.empty else float(df["close"].iloc[-1])

    def _changes(self, symbol: str, price: float, now_ms: int) -> tuple:
        frame = self.store.frame
        return (
            pct_change(price, close_at(frame(symbol, "15m"), "15m", now_ms - HOUR_MS)),
            pct_change(price, close_at(frame(symbol, "1h"), "1h", now_ms - 24 * HOUR_MS)),
            pct_change(price, close_at(frame(symbol, "4h"), "4h", now_ms - 7 * 24 * HOUR_MS)),
        )

    def btc_state(self, now_ms: int) -> BtcState | None:
        if not self.btc_symbol or not self.store.is_loaded(self.btc_symbol):
            return None
        price = self.price(self.btc_symbol)
        if price is None:
            return None
        ch_1h, ch_24h, _ = self._changes(self.btc_symbol, price, now_ms)

        def trend(tf: str) -> ind.Trend | None:
            base = self.base_indicators(self.btc_symbol, tf)
            return ind.trend_label(price, base.ema20, base.ema50, base.ema200) if base else None

        return BtcState(
            price=price,
            change_1h_pct=ind.round_sig(ch_1h, 3),
            change_24h_pct=ind.round_sig(ch_24h, 3),
            trend_1h=trend("1h"),
            trend_4h=trend("4h"),
            trend_1d=trend("1d"),
        )

    def build(self, symbol: str, *, now_ms: int | None = None) -> MarketSnapshot | None:
        if not self.store.is_loaded(symbol):
            return None
        price = self.price(symbol)
        if price is None:
            return None
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms

        timeframes = {}
        for tf in self.store.timeframes:
            base = self.base_indicators(symbol, tf)
            if base is not None:
                timeframes[tf] = ind.derive(base, price)

        ch_1h, ch_24h, ch_7d = self._changes(symbol, price, now_ms)

        recent_high = recent_low = drop = rise = None
        hourly = self.store.frame(symbol, "1h")
        if hourly is not None and not hourly.empty:
            window = hourly.iloc[-self.high_lookback_hours :]
            recent_high = max(float(window["high"].max()), price)
            recent_low = min(float(window["low"].min()), price)
            drop = (recent_high - price) / recent_high * 100
            rise = (price - recent_low) / recent_low * 100

        supports: list[float] = []
        resistances: list[float] = []
        levels_df = self.store.frame(symbol, LEVELS_TIMEFRAME)
        if levels_df is not None and not levels_df.empty:
            tail = levels_df.iloc[-LEVELS_LOOKBACK_CANDLES:]
            supports, resistances = ind.swing_levels(tail["high"], tail["low"], price)

        r = ind.round_sig
        return MarketSnapshot(
            symbol=symbol,
            price=price,
            as_of=now_ms // 1000,
            change_1h_pct=r(ch_1h, 3),
            change_24h_pct=r(ch_24h, 3),
            change_7d_pct=r(ch_7d, 3),
            high_lookback_hours=self.high_lookback_hours,
            recent_high=r(recent_high),
            recent_low=r(recent_low),
            drop_from_high_pct=r(drop, 3),
            rise_from_low_pct=r(rise, 3),
            timeframes=timeframes,
            supports=[r(x) for x in supports],
            resistances=[r(x) for x in resistances],
            btc=self.btc_state(now_ms) if symbol != self.btc_symbol else None,
            derivatives=self.context.derivatives.get(symbol),
            sentiment=self.context.sentiment,
            macro=self.context.macro(),
        )
