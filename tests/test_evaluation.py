import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.evaluation.outcomes import (
    HORIZONS,
    OutcomeEvaluator,
    covers,
    evaluate_path,
    levels_for,
    net_pct,
    window,
)
from bot.evaluation.scoring import (
    build_report,
    latest_answers,
    score_strategies,
    split_by_rules,
    trade_result,
)
from bot.exchange.models import Candle
from bot.settings import Secrets, Settings, load_strategy
from bot.storage.db import Repo, make_engine
from bot.storage.models import AICall, Signal, SignalOutcome
from bot.telegram import stats as stats_commands
from bot.telegram.deps import Deps
from bot.telegram.messages import stats_message

FEE = 0.001
STEP = 900_000
T0 = 1_790_000_000  # signal time (seconds)
STRATEGY = load_strategy()
REAL_RULES = [
    {
        "name": "drop_from_high",
        "label": "Drop",
        "op": ">=",
        "threshold": 6.0,
        "value": 7,
        "passed": True,
    },
    {"name": "rsi_1h", "label": "RSI", "op": "<=", "threshold": 35.0, "value": 30, "passed": True},
    {"name": "btc_change_1h_pct", "value": -0.5},
]
TEST_RULES = [
    {**REAL_RULES[0], "threshold": 0.1},
    {**REAL_RULES[1], "threshold": 99.0},
    REAL_RULES[2],
]
SUGGESTION = {
    "stop": 96.0,
    "targets": [
        {"pct": "10", "price": "110.23", "gain_usdt": "6"},
        {"pct": "15", "price": "115.3"},
    ],
}


def candle(i, high, low, close=None, start=T0 * 1000):
    o = start + i * STEP
    return Candle(o, close or low, high, low, close or (high + low) / 2, 1.0, o + STEP - 1)


def flat_path(n, price=100.0, start=T0 * 1000):
    return [candle(i, price + 0.5, price - 0.5, price, start) for i in range(n)]


def make_signal(
    repo, *, ts=T0, rules=REAL_RULES, suggestion=SUGGESTION, action="none", price=100.0
):
    return repo.add_signal(
        Signal(
            symbol="SOLUSDT",
            ts=ts,
            price=price,
            snapshot_json=json.dumps({"timeframes": {"4h": {"atr": 2.0}}}),
            rule_values_json=json.dumps(rules),
            suggestion_json=json.dumps(suggestion) if suggestion else None,
            high_risk=False,
            user_action=action,
        )
    )


# --- outcomes ----------------------------------------------------------------------------


def test_target_first():
    path = [candle(0, 101, 99), candle(1, 111, 100, 110.5), candle(2, 112, 95, 97)]
    o = evaluate_path(100, 96, 110.23, path, FEE)
    assert o.first_hit == "target"
    assert o.max_favourable_pct == pytest.approx(12)
    assert o.max_adverse_pct == pytest.approx(-5)
    assert o.return_pct == pytest.approx(net_pct(100, 97, FEE))


def test_stop_first_and_same_candle_tie_counts_as_stop():
    assert (
        evaluate_path(100, 96, 110.23, [candle(0, 101, 95.5), candle(1, 112, 100)], FEE).first_hit
        == "stop"
    )
    assert evaluate_path(100, 96, 110.23, [candle(0, 111, 95)], FEE).first_hit == "stop"
    assert evaluate_path(100, 96, 110.23, flat_path(4), FEE).first_hit == "none"


def test_net_pct_includes_both_fees():
    assert net_pct(100, 100, FEE) == pytest.approx((0.999**2 - 1) * 100)


def test_levels_from_suggestion_and_fallback():
    repo = Repo(make_engine(None))
    lv = levels_for(make_signal(repo), STRATEGY)
    assert (lv.stop, lv.target, lv.target_pct) == (96.0, 110.23, 10.0)
    assert lv.stop_loss_pct == pytest.approx(net_pct(100, 96, FEE))
    fallback = levels_for(make_signal(repo, suggestion=None), STRATEGY)
    assert fallback.stop == pytest.approx(96.0)  # 100 - 2.0 ATR * 2
    assert fallback.target == pytest.approx(110.2203, abs=1e-3)


def test_window_excludes_candle_that_closed_as_signal_fired():
    before = candle(-1, 101, 99)
    path = flat_path(100)
    kept = window([before] + path, T0, HORIZONS["24h"])
    assert kept[0].open_time_ms == T0 * 1000 and len(kept) == 96
    assert covers(kept, T0, HORIZONS["24h"])
    assert not covers(kept[:50], T0, HORIZONS["24h"])


# --- evaluator ---------------------------------------------------------------------------


class FakeKlines:
    def __init__(self, fail=0):
        self.calls = []
        self.fail = fail

    async def get_klines_range(self, symbol, interval, start_ms, end_ms):
        self.calls.append((symbol, start_ms, end_ms))
        if self.fail:
            self.fail -= 1
            raise ConnectionError("down")
        n = (end_ms - start_ms) // STEP
        return flat_path(int(n), start=start_ms)


async def test_only_due_horizons_idempotent_and_backfill():
    repo = Repo(make_engine(None))
    s = make_signal(repo)
    clock = SimpleNamespace(now=T0 + HORIZONS["24h"] + 60)
    ev = OutcomeEvaluator(
        repo=repo, klines=FakeKlines(), strategy=STRATEGY, clock=lambda: clock.now
    )
    assert await ev.run_once() == 1
    assert set(repo.outcomes_by_signal()[s.id]) == {"24h"}
    assert await ev.run_once() == 0  # idempotent

    clock.now = T0 + HORIZONS["7d"] + 60  # "downtime": both remaining horizons due at once
    klines = FakeKlines()
    ev.klines = klines
    assert await ev.run_once() == 2
    assert len(klines.calls) == 1  # one fetch covers both horizons
    assert set(repo.outcomes_by_signal()[s.id]) == {"24h", "72h", "7d"}


async def test_fetch_cap_and_retry_after_failure():
    repo = Repo(make_engine(None))
    for i in range(3):
        make_signal(repo, ts=T0 + i)
    now = T0 + HORIZONS["24h"] + 60
    ev = OutcomeEvaluator(
        repo=repo, klines=FakeKlines(fail=1), strategy=STRATEGY, clock=lambda: now, max_fetches=2
    )
    assert await ev.run_once() == 1  # 2 fetches allowed, the first failed
    assert await ev.run_once() == 2  # the rest, including the failed one
    assert len(repo.outcome_keys()) == 3


# --- scoring -------------------------------------------------------------------------------


def outcome(signal_id, first_hit, return_pct=1.0, horizon="7d"):
    return SignalOutcome(
        signal_id=signal_id,
        horizon=horizon,
        return_pct=return_pct,
        max_favourable_pct=5,
        max_adverse_pct=-3,
        first_hit=first_hit,
        target_pct=10.0,
        stop_loss_pct=-4.2,
        evaluated_at=0,
    )


def answer(signal_id, model, action, confidence=0.6, ts=1):
    return AICall(
        ts=ts,
        provider="openrouter",
        model=model,
        purpose="entry",
        role="primary",
        prompt="",
        response=json.dumps({"action": action, "confidence": confidence}),
        valid_json=True,
        signal_id=signal_id,
    )


def test_trade_result():
    assert trade_result(outcome(1, "target")) == 10.0
    assert trade_result(outcome(1, "stop")) == -4.2
    assert trade_result(outcome(1, "none", -1.5)) == -1.5


def test_strategies_on_same_signals():
    repo = Repo(make_engine(None))
    s1 = make_signal(repo, action="entered")  # target
    s2 = make_signal(repo, action="ignored")  # stop
    s3 = make_signal(repo)  # neither, +2%
    outcomes = {
        s1.id: {"7d": outcome(s1.id, "target")},
        s2.id: {"7d": outcome(s2.id, "stop")},
        s3.id: {"7d": outcome(s3.id, "none", 2.0)},
    }
    ai = [
        answer(s1.id, "a/good", "enter"),
        answer(s2.id, "a/good", "skip"),
        answer(s3.id, "a/good", "wait"),
        answer(s1.id, "b/meh", "skip", ts=1),
        answer(s1.id, "b/meh", "enter", ts=2),  # latest wins
    ]
    stats = {
        st.name: st
        for st in score_strategies([s1, s2, s3], outcomes, ai, "7d", {"a/good": (3, 0.03)})
    }

    base = stats["Rules baseline"]
    assert (base.judged, base.trades, base.wins) == (3, 3, 2)
    assert base.total_pct == pytest.approx(10 - 4.2 + 2)
    good = stats["a/good"]
    assert (good.judged, good.trades, good.total_pct) == (3, 1, 10.0)
    assert good.losses_avoided == 1 and good.correct == 2  # right on s1 and s2, wrong on s3
    assert good.cost_per_call == pytest.approx(0.01) and good.avg_confidence == pytest.approx(0.6)
    meh = stats["b/meh"]
    assert (meh.judged, meh.trades, meh.total_pct) == (1, 1, 10.0)  # only the signal it answered
    you = stats["You"]
    assert (you.judged, you.trades, you.total_pct, you.losses_avoided) == (2, 1, 10.0, 1)


def test_latest_answers_skips_bad_json():
    rows = [answer(1, "m", "enter")]
    rows.append(
        AICall(
            ts=5,
            provider="x",
            model="m",
            purpose="entry",
            prompt="",
            response="oops",
            valid_json=True,
            signal_id=2,
        )
    )
    assert latest_answers(rows) == {"m": {1: ("enter", 0.6)}}


def test_test_signals_excluded_by_default():
    repo = Repo(make_engine(None))
    real, test = make_signal(repo), make_signal(repo, rules=TEST_RULES)
    kept, excluded = split_by_rules([real, test], STRATEGY.dip_rules, include_all=False)
    assert kept == [real] and excluded == 1
    assert split_by_rules([real, test], STRATEGY.dip_rules, include_all=True) == ([real, test], 0)


def test_report_uses_longest_available_horizon():
    repo = Repo(make_engine(None))
    s = make_signal(repo)
    report = build_report(
        signals=[s],
        outcomes={s.id: {"24h": outcome(s.id, "none", 1.2, "24h")}},
        ai_rows=[],
        ai_costs={},
        closed_positions=[],
        rules=STRATEGY.dip_rules,
    )
    assert report.horizon == "24h"
    assert report.horizons[0].evaluated == 1 and report.horizons[2].pending == 1


# --- /stats --------------------------------------------------------------------------------


def stats_deps(repo):
    settings = Settings(secrets=Secrets(_env_file=None), strategy=STRATEGY)
    return Deps(client=MagicMock(), stream=SimpleNamespace(ticks={}), settings=settings, repo=repo)


async def run_stats(repo, args=None):
    update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()))
    ctx = SimpleNamespace(bot_data={"deps": stats_deps(repo)}, args=args or [])
    await stats_commands.stats_cmd(update, ctx)
    return update.effective_message.reply_text.await_args.args[0]


async def test_stats_empty():
    text = await run_stats(Repo(make_engine(None)))
    assert "None yet." in text and "Not enough data yet" in text


async def test_stats_with_data_and_all():
    from tests.test_tracker import add_position

    repo = Repo(make_engine(None))
    real = make_signal(repo)
    test = make_signal(repo, rules=TEST_RULES)
    for s in (real, test):
        repo.add_outcome(outcome(s.id, "target", horizon="24h"))
    repo.add_ai_call(answer(real.id, "anthropic/claude-sonnet-5", "enter"))
    p = add_position(repo)
    repo.close_position(
        p.id, exit_time=100, exit_price=110.0, realised_pnl_pct=9.8, realised_pnl_usdt=9.8
    )

    text = await run_stats(repo)
    assert "Paper: 1 trades · win rate 100%" in text
    assert "1 made under different rule thresholds excluded" in text
    assert "Rules baseline" in text and "claude-sonnet-5*" in text  # * = too few answers
    assert "+24h: 1 done" in text

    everything = await run_stats(repo, ["all"])
    assert "(all signals)" in everything and "+24h: 2 done" in everything


def test_stats_message_escapes_and_renders_table():
    report = build_report(
        signals=[],
        outcomes={},
        ai_rows=[],
        ai_costs={},
        closed_positions=[],
        rules=STRATEGY.dip_rules,
    )
    assert "Signals</b>: 0" in stats_message(report)
