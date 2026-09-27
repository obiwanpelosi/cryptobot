from decimal import Decimal as D

from bot.exchange.models import Balance, portfolio_value_usdt


def test_portfolio_value_includes_locked_and_quote():
    balances = {
        "USDT": Balance("USDT", D("100"), D("5")),
        "SOL": Balance("SOL", D("1"), D("0.5")),
        "LINK": Balance("LINK", D("10"), D("0")),
    }
    prices = {"SOLUSDT": D("150"), "LINKUSDT": D("15")}
    total, unpriced = portfolio_value_usdt(balances, prices)
    assert total == D("105") + D("225") + D("150")
    assert unpriced == []


def test_unpriced_nonzero_asset_reported_zero_ignored():
    balances = {
        "USDT": Balance("USDT", D("10"), D("0")),
        "SOL": Balance("SOL", D("2"), D("0")),
        "LINK": Balance("LINK", D("0"), D("0")),
    }
    total, unpriced = portfolio_value_usdt(balances, {})
    assert total == D("10")
    assert unpriced == ["SOL"]
