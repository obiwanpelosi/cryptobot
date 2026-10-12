"""Public Binance price history (no API key, any region), with paging and an on-disk cache."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

from bot.exchange.models import INTERVAL_MS, Candle
from bot.settings import PROJECT_ROOT

log = logging.getLogger(__name__)

# Binance's public market-data mirror: same klines format as api.binance.com, no API key, and
# reachable from regions where api.binance.com answers 451 (restricted location).
KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
PAGE_LIMIT = 1000
CACHE_DIR = PROJECT_ROOT / "data" / "cache"


def _parse(row: list) -> Candle:
    return Candle(
        open_time_ms=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        close_time_ms=int(row[6]),
    )


async def fetch_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    http: httpx.AsyncClient,
    now_ms: int | None = None,
) -> list[Candle]:
    """Closed candles opening in [start_ms, end_ms), fetched 1000 at a time."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    step = INTERVAL_MS[interval]
    candles: dict[int, Candle] = {}
    cursor = start_ms
    while cursor < end_ms:
        response = await http.get(
            KLINES_URL,
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms - 1,
                "limit": PAGE_LIMIT,
            },
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            break
        for candle in map(_parse, rows):
            if candle.open_time_ms < end_ms and candle.close_time_ms < now_ms:
                candles[candle.open_time_ms] = candle
        last_open = int(rows[-1][0])
        if last_open + step <= cursor:  # no progress; stop rather than loop forever
            break
        cursor = last_open + step
    return [candles[k] for k in sorted(candles)]


async def cached_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    http: httpx.AsyncClient,
    cache_dir: Path | None = CACHE_DIR,
) -> list[Candle]:
    """Like fetch_klines, cached on disk by exact range (re-runs in the same hour are instant)."""
    path = None
    if cache_dir is not None:
        path = cache_dir / f"{symbol}_{interval}_{start_ms}_{end_ms}.json"
        if path.exists():
            return [Candle(*row) for row in json.loads(path.read_text())]
    candles = await fetch_klines(symbol, interval, start_ms, end_ms, http=http)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            [c.open_time_ms, c.open, c.high, c.low, c.close, c.volume, c.close_time_ms]
            for c in candles
        ]
        path.write_text(json.dumps(rows))
    log.info("Downloaded %s %s: %d candles", symbol, interval, len(candles))
    return candles
