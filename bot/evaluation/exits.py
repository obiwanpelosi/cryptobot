"""Exit-rule simulation and a fair (walk-forward) comparison of entry + exit setups.

Used by `python -m bot.evaluation.missed_dips --exits --train-end 2024-12-31`.

The best setup is chosen on the TRAIN period only, then checked on the TEST period it never
saw. That guards against picking whatever happened to fit the past best.
"""

from __future__ import annotations

import time
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass, field

from bot.evaluation.outcomes import net_pct
from bot.exchange.models import Candle
from bot.settings import DipRules, StrategyConfig
from bot.signals.sizing import fee_fraction

DAY_MS = 86_400_000
MAX_WORST_PCT = -13.0  # the chosen setup's worst test trade must be no worse than this
MIN_TRAIN_TRADES = 30


@dataclass(frozen=True)
class ExitRule:
    name: str
    stop_k: float = 2.0  # stop = entry - stop_k x ATR(4h)
    target_pct: float | None = None  # net-of-fees target; None = no target
    hold_days: float = 7.0
    trail_after_pct: float | None = None  # start trailing once up this much
    trail_k: float | None = None  # trail at highest - trail_k x ATR, never below breakeven


EXIT_RULES = [
    ExitRule("current: +10% target · 2xATR stop · 7d", target_pct=10),
    ExitRule("+5% target · 2xATR stop · 7d", target_pct=5),
    ExitRule("+7% target · 2xATR stop · 7d", target_pct=7),
    ExitRule("+10% target · 1.5xATR stop · 7d", target_pct=10, stop_k=1.5),
    ExitRule("+10% target · 3xATR stop · 7d", target_pct=10, stop_k=3.0),
    ExitRule("+10% target · 2xATR stop · 3d", target_pct=10, hold_days=3),
    ExitRule("no target · 2xATR stop · hold 7d", hold_days=7),
    ExitRule("no target · 3xATR stop · hold 7d", stop_k=3.0, hold_days=7),
    ExitRule("no target · 2xATR stop · hold 3d", hold_days=3),
    ExitRule("no target · 2xATR stop · hold 5d", hold_days=5),
    ExitRule("trail 2xATR after +5% · 2xATR stop · 7d", trail_after_pct=5, trail_k=2.0),
    ExitRule("trail 1.5xATR after +5% · 2xATR stop · 7d", trail_after_pct=5, trail_k=1.5),
]


def simulate_exit(
    entry: float, atr: float, path: list[Candle], fee: float, rule: ExitRule
) -> float | None:
    """Net % result of one trade, or None if there isn't enough price data yet.

    `path` is the candles from entry onwards (at least `hold_days` of them). Within a candle the
    stop is checked before the target, like outcomes.evaluate_path (conservative).
    """
    if not atr or atr != atr:
        return None
    steps = int(rule.hold_days * 96)
    if len(path) < steps - 2:
        return None
    path = path[:steps]
    stop = entry - rule.stop_k * atr
    if stop <= 0:
        return None
    target = entry * (1 + rule.target_pct / 100) / (1 - fee) ** 2 if rule.target_pct else None
    breakeven = entry / (1 - fee) ** 2
    highest, trailing = entry, False
    for c in path:
        if c.low <= stop:
            return net_pct(entry, stop, fee)
        if target is not None and c.high >= target:
            return float(rule.target_pct)
        highest = max(highest, c.high)
        if (
            rule.trail_after_pct
            and not trailing
            and c.high >= entry * (1 + rule.trail_after_pct / 100)
        ):
            trailing = True
        if trailing:
            stop = max(stop, highest - rule.trail_k * atr, breakeven)
    return net_pct(entry, path[-1].close, fee)


# --- entry sets ------------------------------------------------------------------------------


@dataclass(frozen=True)
class EntrySet:
    name: str
    rules: DipRules
    cooldown_minutes: int


def entry_sets(rules: DipRules) -> list[EntrySet]:
    base = rules.model_copy(
        update={"require_coin_above_ema200_1d": False, "require_btc_above_ema50_1d": False}
    )
    day = 24 * 60
    return [
        EntrySet("current rules (cooldown as config)", base, rules.cooldown_minutes_per_symbol),
        EntrySet("one per 24h", base, day),
        EntrySet(
            "24h + coin > 200d EMA",
            base.model_copy(update={"require_coin_above_ema200_1d": True}),
            day,
        ),
        EntrySet(
            "24h + BTC > 50d EMA", base.model_copy(update={"require_btc_above_ema50_1d": True}), day
        ),
        EntrySet(
            "24h + coin > 200d + BTC > 50d",
            base.model_copy(
                update={"require_coin_above_ema200_1d": True, "require_btc_above_ema50_1d": True}
            ),
            day,
        ),
    ]


# --- walk-forward ------------------------------------------------------------------------------


@dataclass
class Cell:
    entry: str
    exit: str
    train: list[float] = field(default_factory=list)
    test: list[float] = field(default_factory=list)

    @staticmethod
    def _avg(xs: list[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    @property
    def train_avg(self) -> float | None:
        return self._avg(self.train)

    @property
    def test_avg(self) -> float | None:
        return self._avg(self.test)

    @property
    def test_worst(self) -> float | None:
        return min(self.test) if self.test else None

    @staticmethod
    def win_rate(xs: list[float]) -> float | None:
        return sum(x > 0 for x in xs) / len(xs) * 100 if xs else None


def walk_forward(
    data: list,  # list[missed_dips.SymbolData]
    strategy: StrategyConfig,
    *,
    start_ms: int,
    train_end_ms: int,
    find_dips: Callable,
    passes: Callable,
    rules_list: list[ExitRule] = EXIT_RULES,
) -> list[Cell]:
    fee = float(fee_fraction(strategy.fees))
    cells: dict[tuple[str, str], Cell] = {}
    for es in entry_sets(strategy.dip_rules):
        for sd in data:
            s = sd.series
            mask = passes(s, es.rules) & (s["t"] >= start_ms)
            for idx in find_dips(s["t"], mask, es.cooldown_minutes):
                t = int(s["t"].iloc[idx])
                entry = float(s["price"].iloc[idx])
                atr = float(s["atr_4h"].iloc[idx])
                lo = bisect_left(sd.open_times, t - 60_000)
                hi = bisect_left(
                    sd.open_times, t + int(max(r.hold_days for r in rules_list) * DAY_MS)
                )
                path = [c for c in sd.path[lo:hi] if c.open_time_ms >= t - 60_000]
                for rule in rules_list:
                    result = simulate_exit(entry, atr, path, fee, rule)
                    if result is None:
                        continue
                    cell = cells.setdefault((es.name, rule.name), Cell(es.name, rule.name))
                    (cell.train if t <= train_end_ms else cell.test).append(result)
    return list(cells.values())


def choose(cells: list[Cell]) -> tuple[Cell | None, Cell | None]:
    """(best on TRAIN with enough trades, the current setup) — test results are not used to pick."""
    eligible = [c for c in cells if len(c.train) >= MIN_TRAIN_TRADES and c.train_avg is not None]
    best = max(eligible, key=lambda c: c.train_avg, default=None)
    current = next(
        (c for c in cells if c.entry.startswith("current") and c.exit.startswith("current")), None
    )
    return best, current


def verdict(best: Cell | None, current: Cell | None) -> tuple[bool, str]:
    if best is None or current is None or not best.test or not current.test:
        return False, "FAIL: not enough data in the test period to judge."
    beats = best.test_avg > current.test_avg
    safe = best.test_worst is not None and best.test_worst >= MAX_WORST_PCT
    if beats and safe:
        return True, (
            f"PASS: chosen on train, it also beat the current setup on the unseen test period "
            f"({best.test_avg:+.2f}% vs {current.test_avg:+.2f}% per trade; worst test trade "
            f"{best.test_worst:+.1f}%)."
        )
    reasons = []
    if not beats:
        reasons.append(
            f"test avg {best.test_avg:+.2f}% didn't beat current {current.test_avg:+.2f}%"
        )
    if not safe:
        reasons.append(f"worst test trade {best.test_worst:+.1f}% is below {MAX_WORST_PCT:.0f}%")
    return False, "FAIL: " + "; ".join(reasons) + "."


def _p(v: float | None) -> str:
    return "-" if v is None else f"{v:+.2f}%"


def render_walk_forward(cells: list[Cell], train_end_ms: int, top: int = 12) -> str:
    end = time.strftime("%Y-%m-%d", time.gmtime(train_end_ms / 1000))
    best, current = choose(cells)
    ok, text = verdict(best, current)
    lines = [
        f"## Fair test: chosen on data up to {end}, checked on data after it",
        "",
        f"**{text}**",
        "",
        f"Top {top} setups by TRAIN average (min {MIN_TRAIN_TRADES} train trades), with their"
        " unseen TEST results. The current setup is listed last for comparison.",
        "",
        "| entry | exit | train trades | train avg | test trades | test avg | test win"
        " | test worst |",
        "|---|---|---|---|---|---|---|---|",
    ]
    ranked = sorted(
        (c for c in cells if len(c.train) >= MIN_TRAIN_TRADES),
        key=lambda c: c.train_avg,
        reverse=True,
    )[:top]
    if current is not None and current not in ranked:
        ranked.append(current)
    for c in ranked:
        mark = " ⭐" if c is best else (" (current)" if c is current else "")
        win = Cell.win_rate(c.test)
        lines.append(
            f"| {c.entry}{mark} | {c.exit} | {len(c.train)} | {_p(c.train_avg)} | {len(c.test)}"
            f" | {_p(c.test_avg)} | {'-' if win is None else f'{win:.0f}%'} | {_p(c.test_worst)} |"
        )
    return "\n".join(lines)
