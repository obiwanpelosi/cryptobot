"""Open and close positions. Shared by alert buttons and /enter, /close."""

from __future__ import annotations

import logging
import time
from decimal import ROUND_FLOOR, Decimal

from bot.positions.logic import realised
from bot.signals.sizing import fee_fraction, round_to
from bot.storage.models import Position
from bot.telegram.deps import Deps

log = logging.getLogger(__name__)


def atr_4h(deps: Deps, symbol: str) -> float | None:
    if deps.snapshots is None:
        return None
    base = deps.snapshots.base_indicators(symbol, "4h")
    return base.atr if base else None


def default_stop(deps: Deps, symbol: str, entry: Decimal) -> Decimal | None:
    """Spec stop: entry - ATR_4h * stop_atr_multiplier, rounded down to the tick size."""
    atr = atr_4h(deps, symbol)
    if atr is None:
        return None
    mult = Decimal(str(deps.settings.strategy.exit_alerts.stop_atr_multiplier))
    stop = entry - Decimal(str(atr)) * mult
    if symbol in deps.filters:
        stop = round_to(stop, deps.filters[symbol].tick_size, ROUND_FLOOR)
    return stop if stop > 0 else None


def open_position(
    deps: Deps,
    symbol: str,
    usdt: Decimal,
    price: Decimal,
    *,
    stop: Decimal | None = None,
    signal_id: int | None = None,
) -> Position:
    fee = fee_fraction(deps.settings.strategy.fees)
    qty = usdt / price * (1 - fee)  # Binance takes the buy fee from the coin received
    if symbol in deps.filters:
        qty = round_to(qty, deps.filters[symbol].step_size, ROUND_FLOOR)
    if stop is None:
        stop = default_stop(deps, symbol, price)
    position = deps.repo.add_position(
        Position(
            symbol=symbol,
            entry_time=int(time.time()),
            entry_price=float(price),
            amount_usdt=float(usdt),
            quantity=float(qty),
            stop_price=float(stop) if stop else None,
            fees_usdt=float(usdt * fee),
            linked_signal_id=signal_id,
            is_paper=deps.mode == "paper",
            highest_price=float(price),
        )
    )
    log.info(
        "Position #%s opened: %s %s USDT @ %s stop=%s signal=%s paper=%s",
        position.id,
        symbol,
        usdt,
        price,
        stop,
        signal_id,
        position.is_paper,
    )
    if deps.tracker is not None:
        deps.tracker.reload()
    return position


def preview_close(deps: Deps, position: Position, price: float) -> dict:
    fee = float(fee_fraction(deps.settings.strategy.fees))
    return realised(position.amount_usdt, position.quantity, price, fee)


def close_position(deps: Deps, position_id: int, price: float) -> Position | None:
    """Close at `price`. Returns the updated position, or None if missing/already closed."""
    position = deps.repo.get_position(position_id)
    if position is None or position.status != "open":
        return None
    r = preview_close(deps, position, price)
    ok = deps.repo.close_position(
        position_id,
        exit_time=int(time.time()),
        exit_price=r["exit_price"],
        realised_pnl_usdt=r["realised_pnl_usdt"],
        realised_pnl_pct=r["realised_pnl_pct"],
        fees_usdt=position.fees_usdt + r["sell_fee_usdt"],
    )
    if not ok:
        return None
    log.info(
        "Position #%s closed @ %s: P/L %.2f USDT (%.2f%%)",
        position_id,
        price,
        r["realised_pnl_usdt"],
        r["realised_pnl_pct"],
    )
    if deps.tracker is not None:
        deps.tracker.reload()
    return deps.repo.get_position(position_id)
