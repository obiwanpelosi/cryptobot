import pandas as pd
import pytest

from bot.evaluation import missed_dips as md
from bot.evaluation.exits import (
    Cell,
    ExitRule,
    choose,
    entry_sets,
    render_walk_forward,
    simulate_exit,
    verdict,
    walk_forward,
)
from bot.evaluation.outcomes import net_pct
from bot.exchange.models import INTERVAL_MS, Candle
from bot.settings import load_strategy

FEE = 0.001
STEP = INTERVAL_MS["15m"]
T0 = 1_790_000_000_000 - 1_790_000_000_000 % STEP
STRATEGY = load_strategy()


def path(prices, highs=None, lows=None, start=T0):
    out = []
    for i, p in enumerate(prices):
        o = start + i * STEP
        hi = highs[i] if highs else p
        lo = lows[i] if lows else p
        out.append(Candle(o, p, hi, lo, p, 1.0, o + STEP - 1))
    return out


WEEK = 7 * 96


def test_target_exit():
    prices = [100.0] * 10 + [111.0] * (WEEK - 10)
    assert simulate_exit(100, 2.0, path(prices), FEE, ExitRule("t", target_pct=10)) == 10.0


def test_stop_exit_and_tie_counts_as_stop():
    prices = [100.0] * 10 + [95.0] * (WEEK - 10)
    stop_result = net_pct(100, 96, FEE)
    assert simulate_exit(
        100, 2.0, path(prices), FEE, ExitRule("s", target_pct=10)
    ) == pytest.approx(stop_result)
    tie = path([100.0] * WEEK, highs=[112.0] * WEEK, lows=[95.0] * WEEK)
    assert simulate_exit(100, 2.0, tie, FEE, ExitRule("t", target_pct=10)) == pytest.approx(
        stop_result
    )


def test_time_exit_without_target():
    prices = [100.0 + i * 0.01 for i in range(WEEK)]  # slow drift up, never stopped
    result = simulate_exit(100, 2.0, path(prices), FEE, ExitRule("hold"))
    assert result == pytest.approx(net_pct(100, prices[-1], FEE))
    three_days = simulate_exit(100, 2.0, path(prices), FEE, ExitRule("hold3", hold_days=3))
    assert three_days == pytest.approx(net_pct(100, prices[3 * 96 - 1], FEE))


def test_trailing_stop_locks_in_and_never_below_breakeven():
    up = [100.0 + i * 0.1 for i in range(200)]  # rises to ~119.9
    down = [up[-1] - i * 0.2 for i in range(WEEK - 200)]
    rule = ExitRule("trail", trail_after_pct=5, trail_k=2.0)
    result = simulate_exit(100, 2.0, path(up + down), FEE, rule)
    assert result == pytest.approx(net_pct(100, 119.9 - 4.0, FEE), abs=0.05)
    assert result > 0


def test_not_enough_data_or_bad_atr():
    assert simulate_exit(100, 2.0, path([100.0] * 50), FEE, ExitRule("x")) is None
    assert simulate_exit(100, float("nan"), path([100.0] * WEEK), FEE, ExitRule("x")) is None


def test_entry_sets_use_trend_flags():
    sets = {e.name: e for e in entry_sets(STRATEGY.dip_rules)}
    both = sets["24h + coin > 200d + BTC > 50d"]
    assert both.rules.require_coin_above_ema200_1d and both.rules.require_btc_above_ema50_1d
    assert both.cooldown_minutes == 1440
    assert not sets["current rules (cooldown as config)"].rules.require_coin_above_ema200_1d


def test_choose_uses_train_only_and_verdict():
    current = Cell("current rules (x)", "current: +10%", train=[0.0] * 40, test=[0.1] * 40)
    good_train_bad_test = Cell("a", "x", train=[2.0] * 40, test=[-1.0] * 40)
    other = Cell("b", "y", train=[1.0] * 40, test=[3.0] * 40)
    best, cur = choose([current, good_train_bad_test, other])
    assert best is good_train_bad_test and cur is current  # picked without peeking at test
    ok, text = verdict(best, cur)
    assert not ok and "didn't beat current" in text
    ok, text = verdict(other, cur)
    assert ok and text.startswith("PASS")
    risky = Cell("c", "z", train=[3.0] * 40, test=[2.0] * 39 + [-20.0])
    assert not verdict(risky, cur)[0]


def test_walk_forward_splits_by_date():
    n = 3 * WEEK
    series = pd.DataFrame(
        {
            "t": [T0 + (i + 1) * STEP for i in range(n)],
            "price": [100.0] * n,
            "drop_pct": [7.0 if i in (5, WEEK + 5) else 0.0 for i in range(n)],
            "rsi_1h": [30.0] * n,
            "pct_b_4h": [0.5] * n,
            "atr_4h": [2.0] * n,
            "coin_ema200_1d": [90.0] * n,
            "btc_price": [50.0] * n,
            "btc_ema50_1d": [40.0] * n,
        }
    )
    sd = md.SymbolData("SOLUSDT", series, path([100.0] * n, start=T0 + STEP))
    train_end = T0 + WEEK * STEP
    cells = walk_forward(
        [sd],
        STRATEGY,
        start_ms=T0,
        train_end_ms=train_end,
        find_dips=md.find_dips,
        passes=md.passes,
        rules_list=[ExitRule("hold")],
    )
    current = next(c for c in cells if c.entry.startswith("current"))
    assert (len(current.train), len(current.test)) == (1, 1)
    assert "Fair test" in render_walk_forward(cells, train_end)


def test_replay_trend_rules_match_live_definition():
    rules = STRATEGY.dip_rules.model_copy(
        update={"require_coin_above_ema200_1d": True, "require_btc_above_ema50_1d": True}
    )
    s = pd.DataFrame(
        {
            "drop_pct": [7.0, 7.0, 7.0],
            "rsi_1h": [30.0, 30.0, 30.0],
            "pct_b_4h": [0.5] * 3,
            "price": [100.0, 80.0, 100.0],
            "coin_ema200_1d": [90.0, 90.0, None],
            "btc_price": [50.0, 50.0, 50.0],
            "btc_ema50_1d": [40.0, 40.0, 40.0],
        }
    )
    assert md.passes(s, rules).tolist() == [True, False, False]  # below EMA / missing never pass
    assert md.classify(s.iloc[1], rules) == "trend"
