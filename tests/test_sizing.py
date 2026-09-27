from decimal import Decimal as D

import pytest

from bot.settings import ExitAlerts, Fees, Sizing
from bot.signals.sizing import (
    SymbolFilters,
    net_pnl,
    round_to,
    suggest,
    target_price,
)

SIZING = Sizing(risk_per_trade_pct=1.5, max_position_pct=30, min_order_usdt=10)
EXITS = ExitAlerts(profit_targets_pct=[10, 15, 20], stop_atr_multiplier=2.0)
FEES = Fees(taker_pct=0.1)
FILTERS = SymbolFilters(tick_size=D("0.01"), step_size=D("0.001"), min_notional=D("5"))
FEE = D("0.001")


def run(entry="100", atr="2", total="1000", available="1000", filters=FILTERS, sizing=SIZING):
    return suggest(
        entry=D(entry),
        atr_4h=D(atr),
        total_balance=D(total),
        available_usdt=D(available),
        sizing=sizing,
        exits=EXITS,
        fees=FEES,
        filters=filters,
    )


def test_round_to_directions():
    assert round_to(D("96.019"), D("0.01"), "ROUND_FLOOR") == D("96.01")
    assert round_to(D("110.2201"), D("0.01"), "ROUND_CEILING") == D("110.23")
    assert round_to(D("1.23456"), D("0.001"), "ROUND_FLOOR") == D("1.234")


def test_size_from_risk_with_fees_uncapped():
    # stop = 100 - 5*2 = 90 (10% away). risk = 1000*1.5% = 15.
    # Loss fraction after both fees = 1 - 0.9*0.999^2 = 0.1017991 -> size = 147.35 (< 30% cap),
    # qty rounded down to 1.473 -> size 147.30.
    s = run(atr="5")
    assert s.stop == D("90")
    assert s.stop_distance_pct == D("10")
    assert s.risk_usdt == D("15")
    assert s.qty == D("1.473") and s.size_usdt == D("147.300")
    assert s.capped_by is None
    assert not s.too_small


def test_loss_at_stop_never_exceeds_risk_budget():
    for entry, atr in (("100", "5"), ("14.03", "0.345"), ("121.66", "2.17")):
        s = run(
            entry=entry,
            atr=atr,
            total="199",
            available="199",
            sizing=Sizing(risk_per_trade_pct=1.5, max_position_pct=100, min_order_usdt=10),
        )
        assert -s.loss_at_stop_usdt <= s.risk_usdt
        assert -s.loss_at_stop_usdt >= s.risk_usdt * D("0.99")  # and uses nearly all of it


def test_capped_by_max_position_pct():
    # stop 96 (4%): size = 15/0.04 = 375 -> capped to 30% of 1000 = 300.
    s = run()
    assert s.stop == D("96")
    assert s.size_usdt == D("300") and s.capped_by == "max_position_pct"


def test_capped_by_available_usdt():
    s = run(available="50")
    assert s.size_usdt == D("50") and s.capped_by == "available_usdt"


def test_too_small_below_min_order():
    s = run(total="199", available="5")
    assert s.too_small
    assert "below the minimum order of 10.00 USDT" in s.reason
    assert "available usdt" in s.reason
    assert s.targets == []


def test_exchange_min_notional_wins_when_higher():
    sizing = Sizing(risk_per_trade_pct=1.5, max_position_pct=30, min_order_usdt=1)
    filters = SymbolFilters(tick_size=D("0.01"), step_size=D("0.001"), min_notional=D("20"))
    s = run(available="15", filters=filters, sizing=sizing)
    assert s.too_small and "20.00 USDT" in s.reason


def test_qty_rounded_to_step_and_size_recomputed():
    s = run(entry="123.45", atr="5")
    assert s.qty == round_to(s.qty, D("0.001"), "ROUND_FLOOR")
    assert s.size_usdt == s.qty * D("123.45")


def test_loss_at_stop_after_fees_hand_computed():
    # size 300 at 100, stop 96: qty = 3*0.999 = 2.997; proceeds = 2.997*96*0.999 = 287.424288
    s = run()
    assert s.loss_at_stop_usdt == D("287.424288") - D("300")


def test_targets_net_of_fees():
    s = run()
    t10 = s.targets[0]
    assert t10.pct == D("10")
    assert t10.price == D("110.23")  # 100*1.1/0.999^2 = 110.2203 -> rounded up to tick
    for t in s.targets:
        net_pct = t.gain_usdt / s.size_usdt * 100
        assert net_pct >= t.pct  # rounding up the price only ever helps
        assert abs(net_pct - t.pct) < D("0.02")


def test_net_pnl_and_target_price_identity():
    price = target_price(D("50"), D("15"), FEE)
    assert net_pnl(D("200"), D("50"), price, FEE) == pytest.approx(D("30"), abs=D("1e-9"))


def test_filters_from_exchange_info():
    info = {
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.00100000"},
            {"filterType": "LOT_SIZE", "stepSize": "0.01000000"},
            {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
        ]
    }
    f = SymbolFilters.from_exchange_info(info)
    assert (f.tick_size, f.step_size, f.min_notional) == (D("0.001"), D("0.01"), D("5"))


def test_invalid_stop():
    s = run(atr="60")  # stop would be negative
    assert s.too_small and "valid stop" in s.reason
