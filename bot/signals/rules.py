"""Deterministic dip-signal rules (requirements.md §5.5). Pure functions."""

from __future__ import annotations

from pydantic import BaseModel

from bot.market.snapshot import MarketSnapshot
from bot.settings import DipRules


class Check(BaseModel):
    name: str
    label: str
    value: float | None
    threshold: float
    op: str  # ">=" or "<="
    passed: bool


class RuleResult(BaseModel):
    symbol: str
    fired: bool
    high_risk: bool
    btc_change_1h_pct: float | None
    checks: list[Check]


def _check(name: str, label: str, value: float | None, op: str, threshold: float) -> Check:
    if value is None:
        passed = False  # never fire on missing data (e.g. warm-up)
    elif op == ">=":
        passed = value >= threshold
    else:
        passed = value <= threshold
    return Check(name=name, label=label, value=value, threshold=threshold, op=op, passed=passed)


def evaluate(snapshot: MarketSnapshot, rules: DipRules) -> RuleResult:
    tf_1h = snapshot.timeframes.get("1h")
    tf_4h = snapshot.timeframes.get("4h")
    checks: list[Check] = []

    if rules.min_drop_from_high_pct is not None:
        checks.append(
            _check(
                "drop_from_high",
                f"Drop from {snapshot.high_lookback_hours}h high %",
                snapshot.drop_from_high_pct,
                ">=",
                rules.min_drop_from_high_pct,
            )
        )
    if rules.rsi_1h_max is not None:
        checks.append(
            _check("rsi_1h", "RSI 1h", tf_1h.rsi if tf_1h else None, "<=", rules.rsi_1h_max)
        )
    if rules.require_lower_bollinger_4h:
        checks.append(
            _check(
                "lower_bollinger_4h",
                "Price vs lower 4h Bollinger (%B)",
                tf_4h.bb_pct_b if tf_4h else None,
                "<=",
                0.0,
            )
        )

    btc_change = snapshot.btc.change_1h_pct if snapshot.btc else None
    high_risk = (
        rules.btc_crash_guard_pct_1h is not None
        and btc_change is not None
        and btc_change <= -rules.btc_crash_guard_pct_1h
    )
    return RuleResult(
        symbol=snapshot.symbol,
        fired=bool(checks) and all(c.passed for c in checks),
        high_risk=high_risk,
        btc_change_1h_pct=btc_change,
        checks=checks,
    )


def in_cooldown(last_signal_ts: int | None, now_ts: int, cooldown_minutes: int) -> bool:
    return last_signal_ts is not None and now_ts - last_signal_ts < cooldown_minutes * 60
