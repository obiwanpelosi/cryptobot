"""External market context: futures derivatives, Fear & Greed, macro calendar.

Every fetch is best-effort. A failure keeps the last good value (with its as_of time);
a value that was never fetched is None. Nothing here raises into callers.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from bot.settings import MacroEvent

log = logging.getLogger(__name__)

FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"
DERIVATIVES_REFRESH_SECONDS = 15 * 60
SENTIMENT_REFRESH_SECONDS = 60 * 60
MACRO_WINDOW_DAYS = 7


class Derivatives(BaseModel):
    funding_rate_pct: float | None = None
    next_funding_time_ms: int | None = None
    open_interest_usdt: float | None = None
    oi_change_24h_pct: float | None = None
    as_of: int  # epoch seconds


class Sentiment(BaseModel):
    fear_greed: int
    classification: str
    as_of: int


class DerivativesSource(Protocol):
    async def get_funding(self, symbol: str) -> dict[str, Any]: ...
    async def get_open_interest_change(self, symbol: str) -> dict[str, Any]: ...


def parse_fear_greed(payload: dict[str, Any]) -> tuple[int, str]:
    item = payload["data"][0]
    return int(item["value"]), str(item["value_classification"])


def upcoming_macro(
    events: Iterable[MacroEvent], today: dt.date, days: int = MACRO_WINDOW_DAYS
) -> list[MacroEvent]:
    end = today + dt.timedelta(days=days)
    return sorted((e for e in events if today <= e.date <= end), key=lambda e: e.date)


class MarketContext:
    def __init__(
        self,
        client: DerivativesSource,
        symbols: Iterable[str],
        macro_events: Iterable[MacroEvent] = (),
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._client = client
        self.symbols = list(symbols)
        self.macro_events = list(macro_events)
        self._http = http
        self._clock = clock
        self.derivatives: dict[str, Derivatives] = {}
        self.sentiment: Sentiment | None = None

    async def refresh_derivatives(self) -> None:
        for symbol in self.symbols:
            data: dict[str, Any] = {}
            for fetch in (self._client.get_funding, self._client.get_open_interest_change):
                try:
                    data.update(await fetch(symbol))
                except Exception as exc:
                    log.warning("Derivatives %s(%s) failed: %s", fetch.__name__, symbol, exc)
            if data:
                previous = self.derivatives.get(symbol)
                merged = previous.model_dump() if previous else {}
                merged.update(data, as_of=int(self._clock()))
                self.derivatives[symbol] = Derivatives(**merged)

    async def refresh_sentiment(self) -> None:
        try:
            http = self._http or httpx.AsyncClient(timeout=10)
            try:
                response = await http.get(FEAR_GREED_URL)
                response.raise_for_status()
                value, label = parse_fear_greed(response.json())
            finally:
                if self._http is None:
                    await http.aclose()
        except Exception as exc:
            log.warning("Fear & Greed fetch failed: %s", exc)
            return
        self.sentiment = Sentiment(fear_greed=value, classification=label, as_of=int(self._clock()))

    async def refresh(self) -> None:
        await asyncio.gather(self.refresh_derivatives(), self.refresh_sentiment())

    def macro(self, today: dt.date | None = None) -> list[MacroEvent]:
        return upcoming_macro(self.macro_events, today or dt.date.today())

    async def run(self) -> None:
        """Background refresh loop. Call refresh() once at startup before starting this."""
        elapsed = 0
        while True:
            await asyncio.sleep(DERIVATIVES_REFRESH_SECONDS)
            elapsed += DERIVATIVES_REFRESH_SECONDS
            await self.refresh_derivatives()
            if elapsed % SENTIMENT_REFRESH_SECONDS == 0:
                await self.refresh_sentiment()
