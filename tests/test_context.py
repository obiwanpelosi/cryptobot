import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.exchange.client import open_interest_change
from bot.market.context import MarketContext, parse_fear_greed, upcoming_macro
from bot.settings import MacroEvent


def test_parse_fear_greed():
    payload = {"data": [{"value": "70", "value_classification": "Greed"}]}
    assert parse_fear_greed(payload) == (70, "Greed")


def test_open_interest_change_uses_contracts_and_sorts():
    rows = [
        {"timestamp": 2, "sumOpenInterest": "110", "sumOpenInterestValue": "5000"},
        {"timestamp": 1, "sumOpenInterest": "100", "sumOpenInterestValue": "4000"},
    ]
    result = open_interest_change(rows)
    assert result["oi_change_24h_pct"] == pytest.approx(10.0)
    assert result["open_interest_usdt"] == 5000


def test_upcoming_macro_window():
    today = dt.date(2026, 10, 1)
    events = [
        MacroEvent(date=dt.date(2026, 9, 30), name="past"),
        MacroEvent(date=dt.date(2026, 10, 5), name="CPI"),
        MacroEvent(date=dt.date(2026, 10, 2), name="jobs"),
        MacroEvent(date=dt.date(2026, 10, 20), name="far"),
    ]
    assert [e.name for e in upcoming_macro(events, today)] == ["jobs", "CPI"]


def make_client(funding=None, oi=None):
    client = MagicMock()
    client.get_funding = AsyncMock(
        side_effect=funding if isinstance(funding, Exception) else None,
        return_value=funding or {"funding_rate_pct": 0.01, "next_funding_time_ms": 1},
    )
    client.get_funding.__name__ = "get_funding"
    client.get_open_interest_change = AsyncMock(
        side_effect=oi if isinstance(oi, Exception) else None,
        return_value=oi or {"open_interest_usdt": 1e9, "oi_change_24h_pct": 2.0},
    )
    client.get_open_interest_change.__name__ = "get_open_interest_change"
    return client


async def test_derivatives_refresh_and_keep_last_on_failure():
    ctx = MarketContext(make_client(), ["SOLUSDT"], clock=lambda: 100)
    await ctx.refresh_derivatives()
    d = ctx.derivatives["SOLUSDT"]
    assert d.funding_rate_pct == 0.01 and d.oi_change_24h_pct == 2.0 and d.as_of == 100

    ctx._client = make_client(funding=RuntimeError("x"), oi=RuntimeError("y"))
    await ctx.refresh_derivatives()
    assert ctx.derivatives["SOLUSDT"].funding_rate_pct == 0.01  # kept


async def test_derivatives_none_when_never_fetched():
    ctx = MarketContext(make_client(funding=RuntimeError("x"), oi=RuntimeError("y")), ["SOLUSDT"])
    await ctx.refresh_derivatives()
    assert "SOLUSDT" not in ctx.derivatives


async def test_sentiment_refresh_and_failure_keeps_value():
    response = MagicMock()
    response.json.return_value = {"data": [{"value": "25", "value_classification": "Fear"}]}
    response.raise_for_status.return_value = None
    http = MagicMock()
    http.get = AsyncMock(return_value=response)
    ctx = MarketContext(make_client(), [], http=http, clock=lambda: 5)
    await ctx.refresh_sentiment()
    assert ctx.sentiment.fear_greed == 25 and ctx.sentiment.classification == "Fear"

    http.get = AsyncMock(side_effect=ConnectionError("offline"))
    await ctx.refresh_sentiment()
    assert ctx.sentiment.fear_greed == 25
