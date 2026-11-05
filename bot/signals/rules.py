"""Deterministic dip-signal rules (requirements.md §5.5). Pure functions."""

from __future__ import annotations

from pydantic import BaseModel

from bot.market.snapshot import MarketSnapshot
from bot.settings import DipRules


class Check(BaseModel):
    name: str
    label: str
    value: float | None
    threshold: float | None
    op: str  # ">=", "<=" or ">"
    passed: bool


class RuleResult(BaseModel):
    symbol: str
    fired: bool
    high_risk: bool
    btc_change_1h_pct: float | None
    checks: list[Check]
    # Trend checks that failed while trend_filter_mode is "warn" (the alert still fires).
    against_trend: list[Check] = []


def _check(name: str, label: str, value: float | None, op: str, threshold: float | None) -> Check:
    if value is None or threshold is None:
        passed = False  # never fire on missing data (e.g. warm-up)
    elif op == ">=":
        passed = value >= threshold
    elif op == ">":
        passed = value > threshold
    else:
        passed = value <= threshold
    return Check(name=name, label=label, value=value, threshold=threshold, op=op, passed=passed)


# Trend filters are on/off rules (their threshold is a moving average, not a setting).
TREND_CHECKS = ("coin_above_ema200_1d", "btc_above_ema50_1d")


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

    if rules.require_coin_above_ema200_1d:
        tf_1d = snapshot.timeframes.get("1d")
        checks.append(
            _check(
                "coin_above_ema200_1d",
                "Price vs 200d EMA",
                snapshot.price,
                ">",
                tf_1d.ema200 if tf_1d else None,
            )
        )
    if rules.require_btc_above_ema50_1d:
        btc = snapshot.btc
        checks.append(
            _check(
                "btc_above_ema50_1d",
                "BTC vs 50d EMA",
                btc.price if btc else None,
                ">",
                btc.ema50_1d if btc else None,
            )
        )

    btc_change = snapshot.btc.change_1h_pct if snapshot.btc else None
    high_risk = (
        rules.btc_crash_guard_pct_1h is not None
        and btc_change is not None
        and btc_change <= -rules.btc_crash_guard_pct_1h
    )
    dip_checks = [c for c in checks if c.name not in TREND_CHECKS]
    failed_trend = [c for c in checks if c.name in TREND_CHECKS and not c.passed]
    warn = rules.trend_filter_mode == "warn"
    fired = bool(dip_checks) and all(c.passed for c in dip_checks) and (warn or not failed_trend)
    return RuleResult(
        symbol=snapshot.symbol,
        fired=fired,
        high_risk=high_risk,
        btc_change_1h_pct=btc_change,
        checks=checks,
        against_trend=failed_trend if warn else [],
    )


def in_cooldown(last_signal_ts: int | None, now_ts: int, cooldown_minutes: int) -> bool:
    return last_signal_ts is not None and now_ts - last_signal_ts < cooldown_minutes * 60
