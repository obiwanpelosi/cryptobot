"""Records what the price did after each signal at +24h, +72h and +7d (requirements.md §5.10).

For every signal, acted on or not: return % at the horizon (net of both fees), maximum
favourable and adverse move, and whether the target or the stop would have been hit first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from bot.exchange.models import INTERVAL_MS, Candle
from bot.settings import StrategyConfig
from bot.signals.sizing import fee_fraction, target_price
from bot.storage.db import Repo
from bot.storage.models import Signal, SignalOutcome

log = logging.getLogger(__name__)

HORIZONS: dict[str, int] = {"24h": 24 * 3600, "72h": 72 * 3600, "7d": 7 * 24 * 3600}
PATH_INTERVAL = "15m"
RUN_EVERY_SECONDS = 15 * 60
MAX_FETCHES_PER_PASS = 20


class KlineRangeSource(Protocol):
    async def get_klines_range(
        self, symbol: str, interval: str, start_ms: int, end_ms: int
    ) -> list[Candle]: ...


@dataclass(frozen=True)
class Levels:
    stop: float
    target: float
    target_pct: float
    stop_loss_pct: float  # net % at the stop (negative)


@dataclass(frozen=True)
class Outcome:
    return_pct: float
    max_favourable_pct: float
    max_adverse_pct: float
    first_hit: str  # target | stop | none


def net_pct(entry: float, exit_price: float, fee: float) -> float:
    """Net % of a round trip bought at `entry`, sold at `exit_price`, paying `fee` twice."""
    return ((1 - fee) ** 2 * exit_price / entry - 1) * 100


def levels_for(signal: Signal, strategy: StrategyConfig) -> Levels | None:
    """Stop and first target the signal was judged against (from its saved suggestion)."""
    fee = float(fee_fraction(strategy.fees))
    entry = signal.price
    first_pct = min(strategy.exit_alerts.profit_targets_pct)
    stop = target = None
    target_pct = first_pct
    if signal.suggestion_json:
        suggestion = json.loads(signal.suggestion_json)
        stop = float(suggestion.get("stop") or 0) or None
        targets = suggestion.get("targets") or []
        if targets:
            first = min(targets, key=lambda t: float(t["pct"]))
            target, target_pct = float(first["price"]), float(first["pct"])
    if stop is None:
        try:
            atr = json.loads(signal.snapshot_json)["timeframes"]["4h"]["atr"]
        except (KeyError, TypeError, ValueError):
            atr = None
        if not atr:
            return None
        stop = entry - atr * strategy.exit_alerts.stop_atr_multiplier
    if stop <= 0 or stop >= entry:
        return None
    if target is None:
        target = float(
            target_price(Decimal(str(entry)), Decimal(str(target_pct)), Decimal(str(fee)))
        )
    return Levels(stop, target, target_pct, net_pct(entry, stop, fee))


def evaluate_path(
    entry: float, stop: float, target: float, candles: Sequence[Candle], fee: float
) -> Outcome:
    """Walk candles in time order. A candle touching both levels counts as the stop."""
    first_hit = "none"
    for c in candles:
        hit_stop, hit_target = c.low <= stop, c.high >= target
        if hit_stop:
            first_hit = "stop"
            break
        if hit_target:
            first_hit = "target"
            break
    highest = max(c.high for c in candles)
    lowest = min(c.low for c in candles)
    return Outcome(
        return_pct=net_pct(entry, candles[-1].close, fee),
        max_favourable_pct=(highest - entry) / entry * 100,
        max_adverse_pct=(lowest - entry) / entry * 100,
        first_hit=first_hit,
    )


def window(candles: Sequence[Candle], signal_ts: int, horizon_seconds: int) -> list[Candle]:
    """Candles opening after the signal (the one that closed as it fired is excluded)."""
    start_ms = signal_ts * 1000 - 60_000
    end_ms = (signal_ts + horizon_seconds) * 1000
    return [c for c in candles if start_ms <= c.open_time_ms < end_ms]


def covers(candles: Sequence[Candle], signal_ts: int, horizon_seconds: int) -> bool:
    if not candles:
        return False
    end_ms = (signal_ts + horizon_seconds) * 1000
    return candles[-1].close_time_ms >= end_ms - 2 * INTERVAL_MS[PATH_INTERVAL]


class OutcomeEvaluator:
    def __init__(
        self,
        *,
        repo: Repo,
        klines: KlineRangeSource,
        strategy: StrategyConfig,
        clock: Callable[[], float] = time.time,
        max_fetches: int = MAX_FETCHES_PER_PASS,
    ):
        self.repo = repo
        self.klines = klines
        self.strategy = strategy
        self.fee = float(fee_fraction(strategy.fees))
        self.clock = clock
        self.max_fetches = max_fetches

    async def run_once(self) -> int:
        """Evaluate every due (signal, horizon) pair, fetching one path per signal."""
        now = int(self.clock())
        due: dict[int, tuple[Signal, list[str]]] = {}
        for signal, horizon in self.repo.signals_missing_outcome(HORIZONS, now):
            due.setdefault(signal.id, (signal, []))[1].append(horizon)

        inserted = fetches = 0
        for signal, horizons in due.values():
            if fetches >= self.max_fetches:
                break
            levels = levels_for(signal, self.strategy)
            if levels is None:
                log.warning("Outcome: no usable stop/target for signal #%s; skipping", signal.id)
                continue
            longest = max(HORIZONS[h] for h in horizons)
            try:
                fetches += 1
                candles = await self.klines.get_klines_range(
                    signal.symbol,
                    PATH_INTERVAL,
                    signal.ts * 1000 - 60_000,
                    (signal.ts + longest) * 1000,
                )
            except Exception as exc:
                log.warning("Outcome fetch for signal #%s failed: %s", signal.id, exc)
                continue
            for horizon in horizons:
                seconds = HORIZONS[horizon]
                path = window(candles, signal.ts, seconds)
                if not covers(path, signal.ts, seconds):
                    log.info(
                        "Outcome #%s %s: price data incomplete, retry later", signal.id, horizon
                    )
                    continue
                outcome = evaluate_path(signal.price, levels.stop, levels.target, path, self.fee)
                if self.repo.add_outcome(
                    SignalOutcome(
                        signal_id=signal.id,
                        horizon=horizon,
                        return_pct=outcome.return_pct,
                        max_favourable_pct=outcome.max_favourable_pct,
                        max_adverse_pct=outcome.max_adverse_pct,
                        first_hit=outcome.first_hit,
                        stop_price=levels.stop,
                        target_price=levels.target,
                        target_pct=levels.target_pct,
                        stop_loss_pct=levels.stop_loss_pct,
                        evaluated_at=now,
                    )
                ):
                    inserted += 1
        if inserted:
            log.info("Outcomes: evaluated %d (signal, horizon) pairs", inserted)
        return inserted

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("Outcome evaluation pass failed")
            await asyncio.sleep(RUN_EVERY_SECONDS)
