"""Plain data types shared by the exchange layer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

QUOTE_ASSET = "USDT"


@dataclass(frozen=True)
class Tick:
    symbol: str
    price: Decimal
    change_24h_pct: Decimal
    event_time_ms: int


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int


INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


@dataclass(frozen=True)
class Balance:
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


def portfolio_value_usdt(
    balances: Mapping[str, Balance], prices: Mapping[str, Decimal]
) -> tuple[Decimal, list[str]]:
    """Total value in USDT of `balances`, using `prices` keyed by symbol (e.g. "SOLUSDT").

    Returns (value, unpriced_assets) so the caller can flag non-zero holdings with no price.
    """
    total = Decimal(0)
    unpriced: list[str] = []
    for asset, bal in balances.items():
        if asset == QUOTE_ASSET:
            total += bal.total
            continue
        price = prices.get(f"{asset}{QUOTE_ASSET}")
        if price is None:
            if bal.total:
                unpriced.append(asset)
            continue
        total += bal.total * price
    return total, unpriced
