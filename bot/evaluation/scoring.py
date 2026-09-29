"""Scores the rules baseline, each AI model and the user on the same signals (§5.10). Pure."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field

from bot.settings import DipRules
from bot.storage.models import AICall, Position, Signal, SignalOutcome

HORIZON_ORDER = ["24h", "72h", "7d"]
FULL_HORIZON = "7d"
MIN_ANSWERS_TO_JUDGE = 10


# --- which signals count ---------------------------------------------------------------------


def current_rule_signature(rules: DipRules) -> dict[str, float]:
    sig: dict[str, float] = {}
    if rules.min_drop_from_high_pct is not None:
        sig["drop_from_high"] = float(rules.min_drop_from_high_pct)
    if rules.rsi_1h_max is not None:
        sig["rsi_1h"] = float(rules.rsi_1h_max)
    if rules.require_lower_bollinger_4h:
        sig["lower_bollinger_4h"] = 0.0
    return sig


def signal_rule_signature(signal: Signal) -> dict[str, float]:
    rows = json.loads(signal.rule_values_json or "[]")
    return {r["name"]: float(r["threshold"]) for r in rows if "threshold" in r}


def same_rules(a: dict[str, float], b: dict[str, float]) -> bool:
    return a.keys() == b.keys() and all(abs(a[k] - b[k]) < 1e-9 for k in a)


def split_by_rules(
    signals: Iterable[Signal], rules: DipRules, include_all: bool
) -> tuple[list[Signal], int]:
    """(signals to score, number excluded for being made under different rules)."""
    signals = list(signals)
    if include_all:
        return signals, 0
    current = current_rule_signature(rules)
    kept = [s for s in signals if same_rules(signal_rule_signature(s), current)]
    return kept, len(signals) - len(kept)


# --- one signal's simulated trade -----------------------------------------------------------------


def trade_result(outcome: SignalOutcome) -> float:
    """Net % of entering the signal and exiting at target, stop or the horizon."""
    if outcome.first_hit == "target" and outcome.target_pct is not None:
        return outcome.target_pct
    if outcome.first_hit == "stop" and outcome.stop_loss_pct is not None:
        return outcome.stop_loss_pct
    return outcome.return_pct


# --- strategies ------------------------------------------------------------------------------


@dataclass
class StrategyStats:
    name: str
    judged: int = 0
    trades: int = 0
    wins: int = 0
    total_pct: float = 0.0
    correct: int = 0
    losses_avoided: int = 0
    confidences: list[float] = field(default_factory=list)
    cost_per_call: float | None = None

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.trades * 100 if self.trades else None

    @property
    def avg_pct(self) -> float | None:
        return self.total_pct / self.trades if self.trades else None

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.judged * 100 if self.judged else None

    @property
    def avg_confidence(self) -> float | None:
        return sum(self.confidences) / len(self.confidences) if self.confidences else None

    @property
    def enough_data(self) -> bool:
        return self.judged >= MIN_ANSWERS_TO_JUDGE

    def record(self, entered: bool, outcome: SignalOutcome, confidence: float | None = None):
        result = trade_result(outcome)
        self.judged += 1
        if confidence is not None:
            self.confidences.append(confidence)
        if entered:
            self.trades += 1
            self.total_pct += result
            self.wins += result > 0
            self.correct += result > 0
        else:
            self.correct += result <= 0
            self.losses_avoided += outcome.first_hit == "stop"


def latest_answers(rows: Iterable[AICall]) -> dict[str, dict[int, tuple[str, float]]]:
    """{model: {signal_id: (action, confidence)}} using each model's latest answer."""
    out: dict[str, dict[int, tuple[str, float]]] = {}
    for row in sorted(rows, key=lambda r: (r.ts, r.id or 0)):
        try:
            data = json.loads(row.response or "")
            action, confidence = str(data["action"]), float(data["confidence"])
        except (ValueError, KeyError, TypeError):
            continue
        out.setdefault(row.model, {})[row.signal_id] = (action, confidence)
    return out


def score_strategies(
    signals: Iterable[Signal],
    outcomes: dict[int, dict[str, SignalOutcome]],
    ai_rows: Iterable[AICall],
    horizon: str,
    ai_costs: dict[str, tuple[int, float]] | None = None,
) -> list[StrategyStats]:
    signals = [s for s in signals if horizon in outcomes.get(s.id, {})]
    baseline = StrategyStats("Rules baseline")
    you = StrategyStats("You")
    for s in signals:
        outcome = outcomes[s.id][horizon]
        baseline.record(True, outcome)
        if s.user_action in ("entered", "ignored"):
            you.record(s.user_action == "entered", outcome)

    ids = {s.id for s in signals}
    models = []
    for model, answers in sorted(latest_answers(ai_rows).items()):
        stats = StrategyStats(model)
        for signal_id, (action, confidence) in answers.items():
            if signal_id in ids:
                stats.record(action == "enter", outcomes[signal_id][horizon], confidence)
        if stats.judged:
            calls, cost = (ai_costs or {}).get(model, (0, 0.0))
            stats.cost_per_call = cost / calls if calls else None
            models.append(stats)

    result = [baseline] + models + ([you] if you.judged else [])
    return sorted(result, key=lambda st: st.total_pct, reverse=True)


# --- summaries ---------------------------------------------------------------------------


@dataclass
class HorizonSummary:
    horizon: str
    evaluated: int
    pending: int
    target_first: int
    stop_first: int
    neither: int
    avg_return_pct: float | None
    avg_best_pct: float | None
    avg_worst_pct: float | None


def summarize_horizons(
    signals: list[Signal], outcomes: dict[int, dict[str, SignalOutcome]]
) -> list[HorizonSummary]:
    out = []
    for horizon in HORIZON_ORDER:
        rows = [outcomes[s.id][horizon] for s in signals if horizon in outcomes.get(s.id, {})]

        def avg(values):
            values = list(values)
            return sum(values) / len(values) if values else None

        out.append(
            HorizonSummary(
                horizon=horizon,
                evaluated=len(rows),
                pending=len(signals) - len(rows),
                target_first=sum(r.first_hit == "target" for r in rows),
                stop_first=sum(r.first_hit == "stop" for r in rows),
                neither=sum(r.first_hit == "none" for r in rows),
                avg_return_pct=avg(r.return_pct for r in rows),
                avg_best_pct=avg(r.max_favourable_pct for r in rows),
                avg_worst_pct=avg(r.max_adverse_pct for r in rows),
            )
        )
    return out


def best_horizon(summaries: list[HorizonSummary]) -> str | None:
    """7d if any signal has it, else the longest horizon with data."""
    with_data = [h.horizon for h in summaries if h.evaluated]
    if FULL_HORIZON in with_data:
        return FULL_HORIZON
    return with_data[-1] if with_data else None


@dataclass
class TradeSummary:
    label: str
    count: int
    wins: int
    avg_win_pct: float | None
    avg_loss_pct: float | None
    total_pnl_usdt: float

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.count * 100 if self.count else None


def summarize_trades(positions: Iterable[Position]) -> list[TradeSummary]:
    groups: dict[str, list[Position]] = {}
    for p in positions:
        groups.setdefault("Paper" if p.is_paper else "Real", []).append(p)
    out = []
    for label in ("Real", "Paper"):
        rows = groups.get(label, [])
        if not rows:
            continue
        pcts = [p.realised_pnl_pct or 0.0 for p in rows]
        wins = [x for x in pcts if x > 0]
        losses = [x for x in pcts if x <= 0]
        out.append(
            TradeSummary(
                label=label,
                count=len(rows),
                wins=len(wins),
                avg_win_pct=sum(wins) / len(wins) if wins else None,
                avg_loss_pct=sum(losses) / len(losses) if losses else None,
                total_pnl_usdt=sum(p.realised_pnl_usdt or 0.0 for p in rows),
            )
        )
    return out


@dataclass
class StatsReport:
    trades: list[TradeSummary]
    signals_total: int
    excluded: int
    horizons: list[HorizonSummary]
    horizon: str | None
    strategies: list[StrategyStats]
    include_all: bool


def build_report(
    *,
    signals: list[Signal],
    outcomes: dict[int, dict[str, SignalOutcome]],
    ai_rows: list[AICall],
    ai_costs: dict[str, tuple[int, float]],
    closed_positions: list[Position],
    rules: DipRules,
    include_all: bool = False,
) -> StatsReport:
    kept, excluded = split_by_rules(signals, rules, include_all)
    horizons = summarize_horizons(kept, outcomes)
    horizon = best_horizon(horizons)
    strategies = score_strategies(kept, outcomes, ai_rows, horizon, ai_costs) if horizon else []
    return StatsReport(
        trades=summarize_trades(closed_positions),
        signals_total=len(kept),
        excluded=excluded,
        horizons=horizons,
        horizon=horizon,
        strategies=strategies,
        include_all=include_all,
    )
