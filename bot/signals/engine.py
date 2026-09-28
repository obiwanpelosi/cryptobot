"""Evaluates dip rules on each closed 15m candle and sends entry alerts."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Protocol

from bot.exchange.models import QUOTE_ASSET, Balance, portfolio_value_usdt
from bot.market.snapshot import SnapshotBuilder
from bot.settings import StrategyConfig
from bot.signals.rules import RuleResult, evaluate, in_cooldown
from bot.signals.sizing import Suggestion, SymbolFilters, suggest
from bot.storage.db import Repo
from bot.storage.models import Signal
from bot.telegram.messages import base_asset, entry_alert

log = logging.getLogger(__name__)

SIGNAL_TIMEFRAME = "15m"
EVALUATION_DELAY_SECONDS = 3.0  # let 1h/4h closes at the same moment land first
PAUSED_KEY = "alerts_paused"


class BalanceSource(Protocol):
    async def get_balances(self, assets: list[str]) -> dict[str, Balance]: ...


class AlertSender(Protocol):
    async def send(self, text: str, reply_markup: Any = None) -> int: ...


def alerts_paused(repo: Repo) -> bool:
    return repo.get_state(PAUSED_KEY, "0") == "1"


def set_alerts_paused(repo: Repo, paused: bool) -> None:
    repo.set_state(PAUSED_KEY, "1" if paused else "0")


class SignalEngine:
    def __init__(
        self,
        *,
        strategy: StrategyConfig,
        snapshots: SnapshotBuilder,
        repo: Repo,
        balances: BalanceSource,
        filters: dict[str, SymbolFilters],
        notifier: AlertSender,
        keyboard: Callable[[int, bool], Any] | None = None,
        clock: Callable[[], float] = time.time,
        delay: float = EVALUATION_DELAY_SECONDS,
    ):
        self.cfg = strategy
        self.snapshots = snapshots
        self.repo = repo
        self.balances = balances
        self.filters = filters
        self.notifier = notifier
        self.keyboard = keyboard
        self.clock = clock
        self.delay = delay
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self.advisor: Any = None  # Phase 6: AIAdvisor, or None for rule-only alerts
        self._portfolio: dict[str, Any] = {}

    @property
    def assets(self) -> list[str]:
        return [QUOTE_ASSET] + [base_asset(s) for s in self.cfg.symbols]

    def on_candle_closed(self, symbol: str, tf: str) -> None:
        if tf != SIGNAL_TIMEFRAME or symbol not in self.cfg.symbols:
            return
        task = asyncio.create_task(self._evaluate_later(symbol), name=f"rules-{symbol}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _evaluate_later(self, symbol: str) -> None:
        await asyncio.sleep(self.delay)
        try:
            await self.evaluate_symbol(symbol)
        except Exception:
            log.exception("Rule evaluation failed for %s", symbol)

    async def evaluate_symbol(self, symbol: str) -> Signal | None:
        lock = self._locks.setdefault(symbol, asyncio.Lock())
        async with lock:
            snapshot = self.snapshots.build(symbol)
            if snapshot is None:
                log.info("Rules %s: skipped (not warmed up)", symbol)
                return None
            result = evaluate(snapshot, self.cfg.dip_rules)
            log.info(
                "Rules %s: fired=%s high_risk=%s %s",
                symbol,
                result.fired,
                result.high_risk,
                " ".join(f"{c.name}={c.value}" for c in result.checks),
            )
            if not result.fired:
                return None

            now = int(self.clock())
            last = self.repo.last_signal_time(symbol)
            if in_cooldown(last, now, self.cfg.dip_rules.cooldown_minutes_per_symbol):
                log.info("Rules %s: fired but in cooldown (last signal at %s)", symbol, last)
                return None

            suggestion = await self._suggest(symbol, snapshot.price, snapshot)
            signal = self.repo.add_signal(
                Signal(
                    symbol=symbol,
                    ts=now,
                    price=snapshot.price,
                    snapshot_json=snapshot.model_dump_json(),
                    rule_values_json=json.dumps(
                        [c.model_dump() for c in result.checks]
                        + [{"name": "btc_change_1h_pct", "value": result.btc_change_1h_pct}]
                    ),
                    suggestion_json=suggestion.model_dump_json() if suggestion else None,
                    high_risk=result.high_risk,
                )
            )
            log.info(
                "Signal #%s %s saved: price=%s high_risk=%s size=%s",
                signal.id,
                symbol,
                snapshot.price,
                result.high_risk,
                None if suggestion is None or suggestion.too_small else suggestion.size_usdt,
            )

            if alerts_paused(self.repo):
                log.info("Signal #%s not sent: alerts are paused", signal.id)
                return signal
            await self._send_alert(signal, snapshot, result, suggestion)
            return signal

    async def _suggest(self, symbol: str, price: float, snapshot) -> Suggestion | None:
        atr_4h = snapshot.timeframes.get("4h").atr if snapshot.timeframes.get("4h") else None
        filters = self.filters.get(symbol)
        if atr_4h is None or filters is None:
            log.warning("Sizing for %s unavailable (atr_4h=%s)", symbol, atr_4h)
            return None
        try:
            balances = await self.balances.get_balances(self.assets)
        except Exception as exc:
            log.warning("Sizing for %s: balance fetch failed: %s", symbol, exc)
            return None
        prices = {s: t.price for s, t in self.snapshots.ticks.items()}
        total, _ = portfolio_value_usdt(balances, prices)
        self._portfolio = self.portfolio_summary(total, balances[QUOTE_ASSET].free, prices)
        return suggest(
            entry=Decimal(str(price)),
            atr_4h=Decimal(str(atr_4h)),
            total_balance=total,
            available_usdt=balances[QUOTE_ASSET].free,
            sizing=self.cfg.sizing,
            exits=self.cfg.exit_alerts,
            fees=self.cfg.fees,
            filters=filters,
        )

    def portfolio_summary(self, total, available, prices) -> dict[str, Any]:
        """Compact account state for the AI: totals and open positions with P/L."""
        from bot.positions.logic import position_pnl
        from bot.signals.sizing import fee_fraction

        fee = float(fee_fraction(self.cfg.fees))
        open_positions = []
        for p in self.repo.open_positions():
            price = prices.get(p.symbol)
            pnl_pct = (
                position_pnl(p.amount_usdt, p.quantity, float(price), fee)[1]
                if price is not None
                else None
            )
            open_positions.append(
                {
                    "symbol": p.symbol,
                    "amount_usdt": round(p.amount_usdt, 2),
                    "pnl_pct": None if pnl_pct is None else round(pnl_pct, 2),
                }
            )
        return {
            "total_value_usdt": round(float(total), 2),
            "available_usdt": round(float(available), 2),
            "open_positions": open_positions,
        }

    async def _send_alert(
        self, signal: Signal, snapshot, result: RuleResult, suggestion: Suggestion | None
    ) -> None:
        text = entry_alert(snapshot, result, suggestion)
        has_suggestion = suggestion is not None and not suggestion.too_small
        markup = self.keyboard(signal.id, has_suggestion) if self.keyboard else None
        sent = await self.notifier.send(text, reply_markup=markup)
        if not sent:
            return
        self.repo.mark_alert_sent(signal.id)
        if self.advisor is not None:
            task = asyncio.create_task(
                self._ai_followup(signal, snapshot, result, suggestion, sent, text, markup)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _ai_followup(self, signal, snapshot, result, suggestion, sent, text, markup):
        """Ask the AI about a delivered alert, then append its advice to the same message."""
        from bot.ai.prompts import entry_context, trade_history
        from bot.telegram.messages import ai_entry_section, ai_skipped_line

        try:
            history = trade_history(self.repo.recent_closed_positions(signal.symbol, 20))
            context = entry_context(
                snapshot=snapshot,
                rule_result=result,
                suggestion=suggestion,
                portfolio=self._portfolio,
                history=history,
            )
            outcome = await self.advisor.analyze_entry(context, signal_id=signal.id)
            if not isinstance(sent, list):
                return
            if outcome is None:
                reason = self.advisor.last_skip_reason
                if reason and "limit" in reason:
                    await self.notifier.edit(
                        sent, f"{text}\n\n{ai_skipped_line(reason)}", reply_markup=markup
                    )
                return
            advice, model = outcome
            self.repo.set_signal_ai_advice(
                signal.id, json.dumps({"model": model, **advice.model_dump()})
            )
            section = ai_entry_section(advice, model, suggestion)
            await self.notifier.edit(sent, f"{text}\n\n{section}", reply_markup=markup)
        except Exception:
            log.exception("AI follow-up for signal #%s failed", signal.id)
