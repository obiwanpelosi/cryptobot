"""Watches open positions on every price tick and sends target/stop alerts exactly once."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from bot.exchange.models import Tick
from bot.positions.logic import (
    DueAlert,
    PositionView,
    breakeven_price,
    due_alerts,
    next_target,
    position_pnl,
    trailing_stop,
)
from bot.settings import StrategyConfig
from bot.signals.sizing import fee_fraction
from bot.storage.db import Repo
from bot.storage.models import Position

log = logging.getLogger(__name__)

HIGHEST_WRITE_INTERVAL = 30.0  # seconds between DB writes of a rising highest_price


class Sender(Protocol):
    async def send(self, text: str, reply_markup: Any = None) -> int: ...


def view(p: Position) -> PositionView:
    return PositionView(
        id=p.id,
        symbol=p.symbol,
        entry_price=p.entry_price,
        amount_usdt=p.amount_usdt,
        quantity=p.quantity,
        stop_price=p.stop_price,
        trailing=bool(p.trailing),
        highest_price=p.highest_price,
    )


class PositionTracker:
    def __init__(
        self,
        *,
        repo: Repo,
        strategy: StrategyConfig,
        notifier: Sender,
        atr_4h: Callable[[str], float | None] = lambda symbol: None,
        tick_size: Callable[[str], float] = lambda symbol: 0.0,
        render: Callable[..., tuple[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.repo = repo
        self.cfg = strategy
        self.fee = float(fee_fraction(strategy.fees))
        self.notifier = notifier
        self.atr_4h = atr_4h
        self.tick_size = tick_size
        self.render = render
        self.clock = clock
        self.positions: dict[int, Position] = {}
        self.sent: dict[int, set[str]] = {}
        self._sending: dict[int, set[str]] = {}
        self._last_highest_write: dict[int, float] = {}
        self._tasks: set[asyncio.Task] = set()

    # --- cache -----------------------------------------------------------------------

    def reload(self) -> None:
        self.positions = {p.id: p for p in self.repo.open_positions()}
        self.sent = self.repo.sent_alert_keys(self.positions)

    def load(self) -> None:
        self.reload()
        log.info("Tracking %d open positions", len(self.positions))

    # --- per tick --------------------------------------------------------------------

    def on_tick(self, tick: Tick) -> None:
        price = float(tick.price)
        for position in [p for p in self.positions.values() if p.symbol == tick.symbol]:
            try:
                self._update_position(position, price)
                self._check_alerts(position, price)
            except Exception:
                log.exception("Tracker failed on position #%s", position.id)

    def _update_position(self, p: Position, price: float) -> None:
        stop_changed = False
        highest = max(p.highest_price or p.entry_price, price)
        highest_changed = highest != p.highest_price
        p.highest_price = highest
        if p.trailing:
            new_stop = self.trailing_stop_for(p)
            if p.stop_price is None or new_stop > p.stop_price:
                log.info("Position #%s trailing stop %s -> %s", p.id, p.stop_price, new_stop)
                p.stop_price = new_stop
                stop_changed = True
        now = self.clock()
        last_write = self._last_highest_write.get(p.id, float("-inf"))
        due_write = now - last_write >= HIGHEST_WRITE_INTERVAL
        if stop_changed or (highest_changed and due_write):
            self.repo.update_position(p.id, highest_price=p.highest_price, stop_price=p.stop_price)
            self._last_highest_write[p.id] = now

    def trailing_stop_for(self, p: Position) -> float:
        return trailing_stop(
            current_stop=p.stop_price,
            highest=p.highest_price or p.entry_price,
            atr=self.atr_4h(p.symbol),
            multiplier=self.cfg.exit_alerts.stop_atr_multiplier,
            breakeven=breakeven_price(p.amount_usdt, p.quantity, self.fee),
            tick=self.tick_size(p.symbol),
        )

    def _check_alerts(self, p: Position, price: float) -> None:
        in_flight = self._sending.setdefault(p.id, set())
        due = due_alerts(
            view(p),
            price,
            self.sent.get(p.id, set()) | in_flight,
            targets_pct=self.cfg.exit_alerts.profit_targets_pct,
            trailing_after_pct=self.cfg.exit_alerts.trailing_after_pct,
            fee=self.fee,
        )
        if not due:
            return
        in_flight.update(a.key for a in due)
        task = asyncio.create_task(self._send(p, price, due), name=f"position-alert-{p.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, p: Position, price: float, due: list[DueAlert]) -> None:
        keys = [a.key for a in due]
        try:
            text, markup = self.render_alert(p, price, due)
            if await self.notifier.send(text, reply_markup=markup) < 1:
                log.warning("Position #%s alert %s not delivered; will retry", p.id, keys)
                return
            self.repo.record_alerts(p.id, keys)
            self.sent.setdefault(p.id, set()).update(keys)
            hit = [a.target_pct for a in due if a.kind == "target"]
            if hit:
                targets = sorted(set(json.loads(p.targets_hit or "[]")) | set(hit))
                p.targets_hit = json.dumps(targets)
                self.repo.update_position(p.id, targets_hit=p.targets_hit)
            log.info("Position #%s alert sent: %s at %s", p.id, keys, price)
        except Exception:
            log.exception("Position #%s alert failed", p.id)
        finally:
            self._sending.get(p.id, set()).difference_update(keys)

    def render_alert(self, p: Position, price: float, due: list[DueAlert]) -> tuple[str, Any]:
        pnl_usdt, pnl_pct = position_pnl(p.amount_usdt, p.quantity, price, self.fee)
        context = {
            "position": p,
            "price": price,
            "due": due,
            "pnl_usdt": pnl_usdt,
            "pnl_pct": pnl_pct,
            "next_target": next_target(pnl_pct, self.cfg.exit_alerts.profit_targets_pct),
            "suggested_trailing_stop": self.trailing_stop_for(p),
        }
        if self.render is None:
            return f"Position #{p.id} {p.symbol}: {[a.key for a in due]}", None
        return self.render(**context)
