from decimal import Decimal as D

import pytest

from bot.positions.logic import (
    PositionView,
    breakeven_price,
    due_alerts,
    next_target,
    position_pnl,
    realised,
    trailing_stop,
)
from bot.signals.sizing import target_price

FEE = 0.001
# 100 USDT at 100 -> qty 0.999 (buy fee taken from the coin)
POS = PositionView(
    id=1, symbol="SOLUSDT", entry_price=100, amount_usdt=100, quantity=0.999, stop_price=96
)
TARGETS = [10, 15, 20]


def due(price, sent=(), pos=POS, trailing_after=15):
    return due_alerts(
        pos, price, set(sent), targets_pct=TARGETS, trailing_after_pct=trailing_after, fee=FEE
    )


# --- maths -------------------------------------------------------------------------------


def test_pnl_after_fees_hand_computed():
    # 0.999 * 110 * 0.999 - 100 = 9.78011
    pnl, pct = position_pnl(100, 0.999, 110, FEE)
    assert pnl == pytest.approx(9.78011)
    assert pct == pytest.approx(9.78011)


def test_target_hit_price_matches_entry_alert_target_price():
    price = float(target_price(D("100"), D("10"), D(str(FEE))))
    assert position_pnl(100, 0.999, price, FEE)[1] == pytest.approx(10)


def test_breakeven_and_realised():
    be = breakeven_price(100, 0.999, FEE)
    assert position_pnl(100, 0.999, be, FEE)[0] == pytest.approx(0, abs=1e-9)
    r = realised(100, 0.999, 90, FEE)
    assert r["realised_pnl_usdt"] == pytest.approx(0.999 * 90 * 0.999 - 100)
    assert r["realised_pnl_pct"] == pytest.approx(r["realised_pnl_usdt"])
    assert r["sell_fee_usdt"] == pytest.approx(0.999 * 90 * FEE)


def test_next_target():
    assert next_target(5, TARGETS) == 10
    assert next_target(12, TARGETS) == 15
    assert next_target(25, TARGETS) is None


# --- due alerts ----------------------------------------------------------------------------


def keys(alerts):
    return [a.key for a in alerts]


def test_target_boundary():
    edge = float(target_price(D("100"), D("10"), D(str(FEE))))
    assert keys(due(edge - 0.01)) == []
    assert keys(due(edge + 0.001)) == ["target:10"]


def test_jump_past_two_targets_is_one_batch():
    alerts = due(116)  # +15.77%
    assert keys(alerts) == ["target:10", "target:15", "trail_suggest"]


def test_sent_keys_suppress_repeats():
    assert keys(due(116, sent={"target:10", "target:15", "trail_suggest"})) == []


def test_trail_suggest_not_when_trailing_or_disabled():
    trailing = PositionView(**{**POS.__dict__, "trailing": True})
    assert "trail_suggest" not in keys(due(116, pos=trailing))
    assert "trail_suggest" not in keys(due(116, trailing_after=None))


def test_near_stop_and_hit():
    assert keys(due(97.93)) == []  # just outside 2% of 96 (97.92)
    assert keys(due(97.5)) == ["near_stop:96"]
    assert keys(due(96)) == ["stop_hit:96"]  # at the stop is a hit, not "near"
    assert keys(due(95, sent={"stop_hit:96"})) == []


def test_moved_stop_rearms():
    moved = PositionView(**{**POS.__dict__, "stop_price": 112})
    sent = {"near_stop:96", "stop_hit:96"}
    assert keys(due(111, sent=sent, pos=moved)) == ["target:10", "stop_hit:112"]


def test_no_stop_no_stop_alerts():
    no_stop = PositionView(**{**POS.__dict__, "stop_price": None})
    assert keys(due(50, pos=no_stop)) == []


# --- trailing ------------------------------------------------------------------------------


def test_trailing_follows_highs_never_down_never_below_breakeven():
    be = breakeven_price(100, 0.999, FEE)  # ~100.20
    kw = dict(atr=2.0, multiplier=2.0, breakeven=be, tick=0.01)
    assert trailing_stop(current_stop=96, highest=116, **kw) == 112
    assert trailing_stop(current_stop=112, highest=114, **kw) == 112  # never down
    assert trailing_stop(current_stop=112, highest=120, **kw) == 116
    assert trailing_stop(current_stop=96, highest=103, **kw) == pytest.approx(100.21)
    assert trailing_stop(
        current_stop=96, highest=116, atr=None, multiplier=2.0, breakeven=be, tick=0.01
    ) == pytest.approx(100.21)
