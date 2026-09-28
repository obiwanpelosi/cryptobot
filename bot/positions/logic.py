"""Pure position maths and alert decisions (requirements.md §5.8).

P/L uses the same fee model as entry sizing (bot/signals/sizing.py): `quantity` is stored
net of the buy fee, and selling pays the fee again, so
    value    = qty * price * (1 - f)
    net P/L  = value - amount_usdt
A profit target is "hit" when the net P/L % reaches it, i.e. exactly the target price the
entry alert showed.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

NEAR_STOP_PCT = 2.0


@dataclass(frozen=True)
class PositionView:
    """The fields the alert logic needs (decoupled from the ORM row)."""

    id: int
    symbol: str
    entry_price: float
    amount_usdt: float
    quantity: float
    stop_price: float | None
    trailing: bool = False
    highest_price: float | None = None


@dataclass(frozen=True)
class DueAlert:
    key: str  # unique per position in alerts_sent, e.g. "target:10", "stop_hit:96"
    kind: str  # "target" | "trail_suggest" | "near_stop" | "stop_hit"
    target_pct: float | None = None


def position_pnl(
    amount_usdt: float, quantity: float, price: float, fee: float
) -> tuple[float, float]:
    """(net P/L in USDT, net P/L %) if sold now at `price`."""
    pnl = quantity * price * (1 - fee) - amount_usdt
    return pnl, (pnl / amount_usdt * 100 if amount_usdt else 0.0)


def breakeven_price(amount_usdt: float, quantity: float, fee: float) -> float:
    """Sell price at which net P/L is exactly zero."""
    return amount_usdt / (quantity * (1 - fee))


def realised(amount_usdt: float, quantity: float, exit_price: float, fee: float) -> dict:
    """Fields to store when a position is closed at `exit_price`."""
    proceeds = quantity * exit_price * (1 - fee)
    pnl = proceeds - amount_usdt
    return {
        "exit_price": exit_price,
        "realised_pnl_usdt": pnl,
        "realised_pnl_pct": pnl / amount_usdt * 100 if amount_usdt else 0.0,
        "sell_fee_usdt": quantity * exit_price * fee,
    }


def stop_key(prefix: str, stop: float) -> str:
    return f"{prefix}:{stop:g}"


def due_alerts(
    pos: PositionView,
    price: float,
    sent: set[str],
    *,
    targets_pct: Iterable[float],
    trailing_after_pct: float | None,
    fee: float,
    near_stop_pct: float = NEAR_STOP_PCT,
) -> list[DueAlert]:
    """Alerts that are due at `price` and haven't been sent yet."""
    _, pnl_pct = position_pnl(pos.amount_usdt, pos.quantity, price, fee)
    due: list[DueAlert] = []

    for pct in sorted(targets_pct):
        key = f"target:{pct:g}"
        if pnl_pct >= pct and key not in sent:
            due.append(DueAlert(key, "target", pct))

    if (
        trailing_after_pct is not None
        and not pos.trailing
        and pnl_pct >= trailing_after_pct
        and "trail_suggest" not in sent
    ):
        due.append(DueAlert("trail_suggest", "trail_suggest"))

    if pos.stop_price:
        hit_key = stop_key("stop_hit", pos.stop_price)
        near_key = stop_key("near_stop", pos.stop_price)
        if price <= pos.stop_price:
            if hit_key not in sent:
                due.append(DueAlert(hit_key, "stop_hit"))
        elif price <= pos.stop_price * (1 + near_stop_pct / 100) and near_key not in sent:
            due.append(DueAlert(near_key, "near_stop"))
    return due


def next_target(pnl_pct: float, targets_pct: Iterable[float]) -> float | None:
    return next((t for t in sorted(targets_pct) if pnl_pct < t), None)


def _floor(value: float, tick: float) -> float:
    return math.floor(value / tick + 1e-9) * tick if tick else value


def _ceil(value: float, tick: float) -> float:
    return math.ceil(value / tick - 1e-9) * tick if tick else value


def trailing_stop(
    *,
    current_stop: float | None,
    highest: float,
    atr: float | None,
    multiplier: float,
    breakeven: float,
    tick: float = 0.0,
) -> float:
    """New stop for a trailing position: follows new highs, never below breakeven, never down."""
    candidates = [_ceil(breakeven, tick)]
    if current_stop:
        candidates.append(current_stop)
    if atr:
        candidates.append(_floor(highest - atr * multiplier, tick))
    return round(max(candidates), 10)
