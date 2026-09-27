"""In-memory candle buffers per (symbol, timeframe), kept current from REST + WebSocket."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from typing import Protocol

import pandas as pd

from bot.exchange.models import INTERVAL_MS, Candle

log = logging.getLogger(__name__)

BACKFILL_LIMIT = 100
COLUMNS = ["open", "high", "low", "close", "volume"]


class KlineSource(Protocol):
    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> list[Candle]: ...


def _to_frame(candles: Sequence[Candle]) -> pd.DataFrame:
    frame = pd.DataFrame(
        [[c.open, c.high, c.low, c.close, c.volume] for c in candles],
        index=pd.Index([c.open_time_ms for c in candles], name="open_time_ms"),
        columns=COLUMNS,
        dtype=float,
    )
    return frame[~frame.index.duplicated(keep="last")].sort_index()


class CandleStore:
    def __init__(self, symbols: Iterable[str], timeframes: Iterable[str], max_candles: int = 500):
        self.symbols = list(symbols)
        self.timeframes = list(timeframes)
        self.max_candles = max_candles
        self._frames: dict[tuple[str, str], pd.DataFrame] = {}
        self._versions: dict[tuple[str, str], int] = {}
        self._backfill_lock = asyncio.Lock()

    def keys(self) -> list[tuple[str, str]]:
        return [(s, tf) for s in self.symbols for tf in self.timeframes]

    def is_loaded(self, symbol: str) -> bool:
        return all((symbol, tf) in self._frames for tf in self.timeframes)

    def frame(self, symbol: str, tf: str) -> pd.DataFrame | None:
        return self._frames.get((symbol, tf))

    def version(self, symbol: str, tf: str) -> int:
        return self._versions.get((symbol, tf), 0)

    def _set(self, key: tuple[str, str], frame: pd.DataFrame) -> None:
        self._frames[key] = frame.iloc[-self.max_candles :]
        self._versions[key] = self._versions.get(key, 0) + 1

    def replace(self, symbol: str, tf: str, candles: Sequence[Candle]) -> None:
        self._set((symbol, tf), _to_frame(candles))

    async def load(self, client: KlineSource) -> None:
        keys = self.keys()
        results = await asyncio.gather(
            *(client.get_klines(s, tf, limit=self.max_candles) for s, tf in keys)
        )
        for (symbol, tf), candles in zip(keys, results, strict=True):
            self.replace(symbol, tf, candles)
        log.info("Loaded candles: %d sets x up to %d candles", len(keys), self.max_candles)

    def add(self, symbol: str, tf: str, candle: Candle) -> bool:
        """Append a closed candle. Returns True if a gap was detected (backfill needed)."""
        key = (symbol, tf)
        frame = self._frames.get(key)
        if frame is None:
            return False
        last_open = int(frame.index[-1]) if len(frame) else None
        if last_open is not None and candle.open_time_ms <= last_open:
            return False  # duplicate or older than what we have
        gap = last_open is not None and candle.open_time_ms - last_open > INTERVAL_MS[tf]
        self._set(key, pd.concat([frame, _to_frame([candle])]))
        if gap:
            log.warning("Candle gap detected for %s %s", symbol, tf)
        return gap

    def _merge(self, symbol: str, tf: str, candles: Sequence[Candle]) -> str:
        key = (symbol, tf)
        frame = self._frames.get(key)
        if not candles:
            return "empty"
        if frame is None or not len(frame):
            self.replace(symbol, tf, candles)
            return "reloaded"
        if candles[0].open_time_ms > frame.index[-1] + INTERVAL_MS[tf]:
            return "gap-too-large"
        merged = pd.concat([frame, _to_frame(candles)])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        if len(merged) != len(frame) or not merged.iloc[-1].equals(frame.iloc[-1]):
            self._set(key, merged)
            return "merged"
        return "unchanged"

    async def backfill(self, client: KlineSource) -> None:
        """Fill any gap after a disconnect. Full reload for a key if the gap exceeds the limit."""
        async with self._backfill_lock:
            keys = self.keys()
            results = await asyncio.gather(
                *(client.get_klines(s, tf, limit=BACKFILL_LIMIT) for s, tf in keys),
                return_exceptions=True,
            )
            for (symbol, tf), result in zip(keys, results, strict=True):
                if isinstance(result, BaseException):
                    log.warning("Backfill failed for %s %s: %s", symbol, tf, result)
                    continue
                outcome = self._merge(symbol, tf, result)
                if outcome == "gap-too-large":
                    self.replace(
                        symbol, tf, await client.get_klines(symbol, tf, limit=self.max_candles)
                    )
                    outcome = "reloaded"
                if outcome in ("merged", "reloaded"):
                    log.info("Backfill %s %s: %s", symbol, tf, outcome)
