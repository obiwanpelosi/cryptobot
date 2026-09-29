"""Read-only Binance REST wrapper.

Only read endpoints are exposed. Order placement, withdrawals and transfers are deliberately
not wrapped (see requirements.md §1, §5.1).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from decimal import Decimal
from typing import Any

from binance import AsyncClient
from binance.exceptions import BinanceAPIException

from bot.exchange.models import Balance, Candle, Tick
from bot.settings import Secrets

log = logging.getLogger(__name__)

WEIGHT_LIMIT_1M = 6000
WEIGHT_WARN_RATIO = 0.8
BALANCE_CACHE_SECONDS = 30
DEFAULT_RETRY_AFTER = 60


class BinanceClient:
    def __init__(self, client: AsyncClient, *, clock: Callable[[], float] = time.monotonic):
        self._client = client
        self._clock = clock
        self._balance_cache: tuple[float, dict[str, Balance]] | None = None

    @classmethod
    async def create(cls, secrets: Secrets) -> BinanceClient:
        if not (secrets.binance_api_key and secrets.binance_api_secret):
            raise RuntimeError(
                "BINANCE_API_KEY and BINANCE_API_SECRET (or BINANCE_SECRET_KEY) must be set in .env"
            )
        client = await AsyncClient.create(
            api_key=secrets.binance_api_key.get_secret_value(),
            api_secret=secrets.binance_api_secret.get_secret_value(),
        )
        return cls(client)

    @property
    def raw(self) -> AsyncClient:
        """Underlying client, needed by the WebSocket manager."""
        return self._client

    async def close(self) -> None:
        await self._client.close_connection()

    async def _call(self, fn: Callable[..., Awaitable[Any]], **kwargs: Any) -> Any:
        """Run a request, track used weight, and back off once on 429/418."""
        for attempt in (1, 2):
            try:
                result = await fn(**kwargs)
            except BinanceAPIException as exc:
                if exc.status_code not in (429, 418) or attempt == 2:
                    raise
                wait = _retry_after(exc)
                log.warning(
                    "Binance rate limit hit (HTTP %s), backing off %ss", exc.status_code, wait
                )
                await asyncio.sleep(wait)
                continue
            self._check_weight()
            return result
        raise AssertionError("unreachable")

    def _check_weight(self) -> None:
        response = getattr(self._client, "response", None)
        headers = getattr(response, "headers", None) or {}
        used = headers.get("X-MBX-USED-WEIGHT-1M") or headers.get("x-mbx-used-weight-1m")
        if used is None:
            return
        try:
            used_int = int(used)
        except (TypeError, ValueError):
            return
        if used_int >= WEIGHT_LIMIT_1M * WEIGHT_WARN_RATIO:
            log.warning("Binance request weight high: %s/%s per minute", used_int, WEIGHT_LIMIT_1M)

    async def check_permissions(self) -> dict[str, Any]:
        """Log the API key's permissions. Warns (does not fail) if it is not read-only."""
        perms = await self._call(self._client.get_account_api_permissions)
        risky = [
            name
            for name in (
                "enableWithdrawals",
                "enableSpotAndMarginTrading",
                "enableFutures",
                "enableMargin",
                "enableInternalTransfer",
                "permitsUniversalTransfer",
            )
            if perms.get(name)
        ]
        ip_restricted = bool(perms.get("ipRestrict"))
        log.info(
            "API key: reading=%s ip_restricted=%s", perms.get("enableReading"), ip_restricted
        )
        if risky:
            log.warning(
                "API key is NOT read-only (%s enabled). Disable these in Binance API Management.",
                ", ".join(risky),
            )
        if not ip_restricted:
            log.warning("API key is not IP-restricted. Restrict it to your server's IP if you can.")
        return perms

    async def check_symbols(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Raise if any configured symbol doesn't exist or isn't trading.

        Returns each symbol's exchange-info entry (used for price/quantity filters).
        """
        symbols = list(symbols)
        info = await self._call(self._client.get_exchange_info)
        by_symbol = {s["symbol"]: s for s in info.get("symbols", [])}
        bad = [
            f"{sym} ({by_symbol[sym].get('status') if sym in by_symbol else 'not found'})"
            for sym in symbols
            if by_symbol.get(sym, {}).get("status") != "TRADING"
        ]
        if bad:
            raise RuntimeError(f"Symbols not tradable on Binance spot: {', '.join(bad)}")
        log.info("Symbols OK: %s", ", ".join(symbols))
        return {sym: by_symbol[sym] for sym in symbols}

    async def get_balances(
        self, assets: Iterable[str] = ("USDT", "SOL", "LINK"), *, force: bool = False
    ) -> dict[str, Balance]:
        """Spot wallet balances for `assets`, cached for 30 seconds."""
        now = self._clock()
        cache = self._balance_cache
        if not force and cache and now - cache[0] < BALANCE_CACHE_SECONDS:
            all_balances = cache[1]
        else:
            account = await self._call(self._client.get_account, omitZeroBalances="true")
            all_balances = {
                b["asset"]: Balance(b["asset"], Decimal(b["free"]), Decimal(b["locked"]))
                for b in account.get("balances", [])
            }
            self._balance_cache = (now, all_balances)
        zero = Decimal(0)
        return {a: all_balances.get(a, Balance(a, zero, zero)) for a in assets}

    async def get_24h_tickers(self, symbols: Iterable[str]) -> dict[str, Tick]:
        symbols = list(symbols)
        rows = await self._call(
            self._client.get_ticker, symbols=json.dumps(symbols, separators=(",", ":"))
        )
        return {
            r["symbol"]: Tick(
                symbol=r["symbol"],
                price=Decimal(r["lastPrice"]),
                change_24h_pct=Decimal(r["priceChangePercent"]),
                event_time_ms=int(r["closeTime"]),
            )
            for r in rows
        }

    async def get_klines(
        self, symbol: str, interval: str, limit: int = 500, *, now_ms: int | None = None
    ) -> list[Candle]:
        """Closed candles only: the still-forming last candle is dropped."""
        rows = await self._call(
            self._client.get_klines, symbol=symbol, interval=interval, limit=limit
        )
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        return [candle for candle in map(_parse_kline, rows) if candle.close_time_ms < now_ms]

    async def get_klines_range(
        self, symbol: str, interval: str, start_ms: int, end_ms: int, *, limit: int = 1000
    ) -> list[Candle]:
        """Closed candles opening within [start_ms, end_ms] (up to `limit`)."""
        rows = await self._call(
            self._client.get_klines,
            symbol=symbol,
            interval=interval,
            startTime=start_ms,
            endTime=end_ms,
            limit=limit,
        )
        now_ms = int(time.time() * 1000)
        return [c for c in map(_parse_kline, rows) if c.close_time_ms < now_ms]

    async def get_funding(self, symbol: str) -> dict[str, Any]:
        """Latest funding rate (as %) and next funding time, from USD-M futures (public)."""
        data = await self._call(self._client.futures_mark_price, symbol=symbol)
        return {
            "funding_rate_pct": float(data["lastFundingRate"]) * 100,
            "next_funding_time_ms": int(data["nextFundingTime"]),
        }

    async def get_open_interest_change(self, symbol: str) -> dict[str, Any]:
        """Current open interest (USDT) and its 24h % change, from hourly OI history."""
        rows = await self._call(
            self._client.futures_open_interest_hist, symbol=symbol, period="1h", limit=25
        )
        return open_interest_change(rows)


def _parse_kline(row: list[Any]) -> Candle:
    return Candle(
        open_time_ms=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        close_time_ms=int(row[6]),
    )


def open_interest_change(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """24h change is measured in contracts (sumOpenInterest) so price moves don't distort it."""
    if not rows:
        raise ValueError("empty open interest history")
    rows = sorted(rows, key=lambda r: int(r["timestamp"]))
    first, last = float(rows[0]["sumOpenInterest"]), float(rows[-1]["sumOpenInterest"])
    return {
        "open_interest_usdt": float(rows[-1]["sumOpenInterestValue"]),
        "oi_change_24h_pct": (last - first) / first * 100 if first else 0.0,
    }


def _retry_after(exc: BinanceAPIException) -> int:
    headers = getattr(exc.response, "headers", None) or {}
    try:
        return int(headers.get("Retry-After", DEFAULT_RETRY_AFTER))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER
