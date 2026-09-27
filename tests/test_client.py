import logging
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from binance.exceptions import BinanceAPIException

from bot.exchange import client as client_mod
from bot.exchange.client import BinanceClient

ACCOUNT = {
    "balances": [
        {"asset": "USDT", "free": "100.5", "locked": "0"},
        {"asset": "SOL", "free": "1.2", "locked": "0.3"},
    ]
}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make_client(clock=None):
    raw = MagicMock()
    raw.response = SimpleNamespace(headers={"X-MBX-USED-WEIGHT-1M": "10"})
    raw.get_account = AsyncMock(return_value=ACCOUNT)
    return BinanceClient(raw, clock=clock or Clock()), raw


def api_error(status):
    resp = SimpleNamespace(headers={"Retry-After": "7"}, text="")
    return BinanceAPIException(resp, status, '{"code": -1003, "msg": "Too many requests"}')


async def test_balances_parsed_and_missing_assets_zero():
    bc, _ = make_client()
    bal = await bc.get_balances(["USDT", "SOL", "LINK"])
    assert bal["USDT"].total == D("100.5")
    assert bal["SOL"].free == D("1.2") and bal["SOL"].locked == D("0.3")
    assert bal["LINK"].total == 0


async def test_balance_cache_30s():
    clock = Clock()
    bc, raw = make_client(clock)
    await bc.get_balances()
    clock.now = 29
    await bc.get_balances()
    assert raw.get_account.await_count == 1
    clock.now = 31
    await bc.get_balances()
    assert raw.get_account.await_count == 2


async def test_rate_limit_retries_once(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(client_mod.asyncio, "sleep", fake_sleep)
    bc, raw = make_client()
    raw.get_account = AsyncMock(side_effect=[api_error(429), ACCOUNT])
    await bc.get_balances()
    assert slept == [7]
    assert raw.get_account.await_count == 2


async def test_rate_limit_gives_up_after_retry(monkeypatch):
    monkeypatch.setattr(client_mod.asyncio, "sleep", AsyncMock())
    bc, raw = make_client()
    raw.get_account = AsyncMock(side_effect=[api_error(429), api_error(429)])
    with pytest.raises(BinanceAPIException):
        await bc.get_balances()


async def test_other_api_errors_not_retried():
    bc, raw = make_client()
    raw.get_account = AsyncMock(side_effect=api_error(400))
    with pytest.raises(BinanceAPIException):
        await bc.get_balances()
    assert raw.get_account.await_count == 1


async def test_permission_warning_when_trading_enabled(caplog):
    bc, raw = make_client()
    raw.get_account_api_permissions = AsyncMock(
        return_value={"enableReading": True, "enableSpotAndMarginTrading": True, "ipRestrict": True}
    )
    with caplog.at_level(logging.WARNING):
        await bc.check_permissions()
    assert "NOT read-only" in caplog.text
    assert "enableSpotAndMarginTrading" in caplog.text


async def test_read_only_key_no_warning(caplog):
    bc, raw = make_client()
    raw.get_account_api_permissions = AsyncMock(
        return_value={"enableReading": True, "ipRestrict": True}
    )
    with caplog.at_level(logging.WARNING):
        await bc.check_permissions()
    assert caplog.text == ""


async def test_check_symbols_rejects_unknown():
    bc, raw = make_client()
    raw.get_exchange_info = AsyncMock(
        return_value={"symbols": [{"symbol": "SOLUSDT", "status": "TRADING"}]}
    )
    await bc.check_symbols(["SOLUSDT"])
    with pytest.raises(RuntimeError, match="LINKUSDT"):
        await bc.check_symbols(["SOLUSDT", "LINKUSDT"])


async def test_high_weight_warns(caplog):
    bc, raw = make_client()
    raw.response = SimpleNamespace(headers={"X-MBX-USED-WEIGHT-1M": "5000"})
    with caplog.at_level(logging.WARNING):
        await bc.get_balances()
    assert "weight high" in caplog.text
