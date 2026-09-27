"""Position sizing, stop-loss and fee-aware targets (requirements.md §5.6). Pure Decimal maths.

Fee model: Binance charges `taker_pct` on the buy and again on the sell.
  qty      = size / entry * (1 - f)
  proceeds = qty * exit * (1 - f)
  net P/L  = proceeds - size
Target prices are chosen so the *net* P/L after both fees equals the target %.
Position size is chosen so the *net* loss at the stop equals risk_per_trade_pct of the balance.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from pydantic import BaseModel

from bot.settings import ExitAlerts, Fees, Sizing

HUNDRED = Decimal(100)


class SymbolFilters(BaseModel):
    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal = Decimal(0)

    @classmethod
    def from_exchange_info(cls, info: dict[str, Any]) -> SymbolFilters:
        filters = {f["filterType"]: f for f in info.get("filters", [])}
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        return cls(
            tick_size=Decimal(filters["PRICE_FILTER"]["tickSize"]),
            step_size=Decimal(filters["LOT_SIZE"]["stepSize"]),
            min_notional=Decimal(notional.get("minNotional", "0")),
        )


class Target(BaseModel):
    pct: Decimal
    price: Decimal
    gain_usdt: Decimal


class Suggestion(BaseModel):
    entry: Decimal
    stop: Decimal
    stop_distance_pct: Decimal
    too_small: bool = False
    reason: str | None = None
    size_usdt: Decimal = Decimal(0)
    qty: Decimal = Decimal(0)
    risk_usdt: Decimal = Decimal(0)
    loss_at_stop_usdt: Decimal = Decimal(0)  # negative number, after fees
    targets: list[Target] = []
    capped_by: str | None = None  # "max_position_pct" | "available_usdt" | None


def round_to(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=rounding) * step


def fee_fraction(fees: Fees) -> Decimal:
    return Decimal(str(fees.taker_pct)) / HUNDRED


def net_pnl(size: Decimal, entry: Decimal, exit_price: Decimal, fee: Decimal) -> Decimal:
    qty = size / entry * (1 - fee)
    return qty * exit_price * (1 - fee) - size


def target_price(entry: Decimal, pct: Decimal, fee: Decimal) -> Decimal:
    return entry * (1 + pct / HUNDRED) / (1 - fee) ** 2


def suggest(
    *,
    entry: Decimal,
    atr_4h: Decimal,
    total_balance: Decimal,
    available_usdt: Decimal,
    sizing: Sizing,
    exits: ExitAlerts,
    fees: Fees,
    filters: SymbolFilters,
) -> Suggestion:
    fee = fee_fraction(fees)
    stop = round_to(
        entry - atr_4h * Decimal(str(exits.stop_atr_multiplier)), filters.tick_size, ROUND_FLOOR
    )
    if stop <= 0 or stop >= entry:
        return Suggestion(
            entry=entry,
            stop=stop,
            stop_distance_pct=Decimal(0),
            too_small=True,
            reason="Could not compute a valid stop from ATR.",
        )
    stop_frac = (entry - stop) / entry
    result = Suggestion(entry=entry, stop=stop, stop_distance_pct=stop_frac * HUNDRED)

    risk_usdt = total_balance * Decimal(str(sizing.risk_per_trade_pct)) / HUNDRED
    # Spec formula is risk / stop_frac; using the loss fraction *after both fees* instead keeps
    # the actual loss at the stop within risk_per_trade_pct, as the setting promises.
    loss_frac = 1 - (stop / entry) * (1 - fee) ** 2
    size = risk_usdt / loss_frac
    max_by_pct = total_balance * Decimal(str(sizing.max_position_pct)) / HUNDRED
    capped_by = None
    if size > max_by_pct:
        size, capped_by = max_by_pct, "max_position_pct"
    if size > available_usdt:
        size, capped_by = available_usdt, "available_usdt"

    qty = round_to(size / entry, filters.step_size, ROUND_FLOOR)
    size = qty * entry
    minimum = max(Decimal(str(sizing.min_order_usdt)), filters.min_notional)
    if size < minimum:
        result.too_small = True
        result.reason = (
            f"Suggested size {size:.2f} USDT is below the minimum order of {minimum:.2f} USDT"
            + (f" (limited by {capped_by.replace('_', ' ')})." if capped_by else ".")
        )
        return result

    result.size_usdt = size
    result.qty = qty
    result.risk_usdt = risk_usdt
    result.capped_by = capped_by
    result.loss_at_stop_usdt = net_pnl(size, entry, stop, fee)
    result.targets = targets_for(entry, size, exits.profit_targets_pct, fee, filters.tick_size)
    return result


def targets_for(
    entry: Decimal, size: Decimal, pcts: Iterable[float], fee: Decimal, tick: Decimal
) -> list[Target]:
    out = []
    for pct in pcts:
        p = Decimal(str(pct))
        price = round_to(target_price(entry, p, fee), tick, ROUND_CEILING)
        out.append(Target(pct=p, price=price, gain_usdt=net_pnl(size, entry, price, fee)))
    return out
