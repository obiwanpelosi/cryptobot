"""Live price stream over Binance WebSocket, with our own reconnect loop.

python-binance's ReconnectingWebsocket gives up after 5 attempts and sometimes reports
failures as {"e": "error"} messages rather than exceptions, so this wraps it in an outer
loop with exponential backoff that never gives up.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any, Protocol

from bot.exchange.models import Candle, Tick

log = logging.getLogger(__name__)

BACKOFF_START = 1.0
BACKOFF_MAX = 60.0
RECV_TIMEOUT = 90.0  # no message at all for this long => treat the connection as dead
STALE_AFTER_SECONDS = 300


class SocketFactory(Protocol):
    def __call__(self, streams: list[str]) -> Any:  # returns an async context manager with recv()
        ...


class StreamError(Exception):
    pass


class PriceStream:
    def __init__(
        self,
        symbols: Iterable[str],
        socket_factory: SocketFactory,
        *,
        on_tick: Callable[[Tick], None] | None = None,
        kline_intervals: Iterable[str] = (),
        on_candle: Callable[[str, str, Candle], None] | None = None,
        on_connect: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.symbols = [s.upper() for s in symbols]
        self.kline_intervals = list(kline_intervals)
        self._socket_factory = socket_factory
        self._on_tick = on_tick
        self._on_candle = on_candle
        self._on_connect = on_connect
        self._clock = clock
        self.ticks: dict[str, Tick] = {}
        self.last_message_at: float | None = None
        self.connected = False
        self._backoff = BACKOFF_START
        self._started_at = clock()

    @property
    def stream_names(self) -> list[str]:
        names = [f"{s.lower()}@miniTicker" for s in self.symbols]
        names += [f"{s.lower()}@kline_{i}" for s in self.symbols for i in self.kline_intervals]
        return names

    def seed(self, ticks: dict[str, Tick]) -> None:
        """Prefill prices (e.g. from REST) so there's something to show before the first message."""
        self.ticks.update({s: t for s, t in ticks.items() if s in self.symbols})

    def is_stale(self, threshold: float = STALE_AFTER_SECONDS) -> bool:
        reference = self.last_message_at if self.last_message_at is not None else self._started_at
        return self._clock() - reference > threshold

    def next_backoff(self) -> float:
        """Current backoff delay (with jitter); doubles the next one up to BACKOFF_MAX."""
        delay = self._backoff
        self._backoff = min(self._backoff * 2, BACKOFF_MAX)
        return delay + random.uniform(0, delay * 0.1)

    async def run(self) -> None:
        while True:
            try:
                await self._consume()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                delay = self.next_backoff()
                log.warning("Price stream disconnected (%s); reconnecting in %.1fs", exc, delay)
                await asyncio.sleep(delay)

    async def _consume(self) -> None:
        async with self._socket_factory(self.stream_names) as socket:
            log.info(
                "Price stream connected: %s (%d streams)",
                ", ".join(self.symbols),
                len(self.stream_names),
            )
            if self._on_connect:
                self._on_connect()
            while True:
                msg = await asyncio.wait_for(socket.recv(), timeout=RECV_TIMEOUT)
                self.handle_message(msg)

    def handle_message(self, msg: dict[str, Any]) -> None:
        if not isinstance(msg, dict):
            return
        if msg.get("e") == "error":
            raise StreamError(f"{msg.get('type')}: {msg.get('m')}")
        data = msg.get("data", msg)
        event = data.get("e")
        if event == "kline":
            self._handle_kline(data)
            return
        if event != "24hrMiniTicker":
            return
        close, open_ = Decimal(data["c"]), Decimal(data["o"])
        change = (close - open_) / open_ * 100 if open_ else Decimal(0)
        tick = Tick(
            symbol=data["s"], price=close, change_24h_pct=change, event_time_ms=int(data["E"])
        )
        self.ticks[tick.symbol] = tick
        self.last_message_at = self._clock()
        if not self.connected:
            self.connected = True
        self._backoff = BACKOFF_START
        if self._on_tick:
            self._on_tick(tick)

    def _handle_kline(self, data: dict[str, Any]) -> None:
        self.last_message_at = self._clock()
        self._backoff = BACKOFF_START
        k = data["k"]
        if not k.get("x") or self._on_candle is None:
            return
        candle = Candle(
            open_time_ms=int(k["t"]),
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            close_time_ms=int(k["T"]),
        )
        self._on_candle(k["s"], k["i"], candle)
