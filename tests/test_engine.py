import asyncio
import json
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.exchange.models import Balance, Tick
from bot.settings import load_strategy
from bot.signals.engine import SignalEngine, set_alerts_paused
from bot.signals.sizing import SymbolFilters
from bot.storage.db import Repo, make_engine
from tests.test_rules import snap as make_snap

FILTERS = {"SOLUSDT": SymbolFilters(tick_size=D("0.01"), step_size=D("0.001"), min_notional=D("5"))}


def snapshot(**kw):
    s = make_snap(**kw)
    s.timeframes["4h"].atr = 2.0  # stop = 100 - 2*2 = 96
    return s


class Clock:
    def __init__(self, t=1_000_000):
        self.t = t

    def __call__(self):
        return self.t


def make_engine_under_test(snap=None, usdt="200", clock=None):
    snap = snap or snapshot()
    snapshots = SimpleNamespace(
        build=lambda symbol: snap if symbol == "SOLUSDT" else None,
        ticks={"SOLUSDT": Tick("SOLUSDT", D("100"), D("0"), 0)},
    )
    balances = SimpleNamespace(
        get_balances=AsyncMock(
            return_value={
                "USDT": Balance("USDT", D(usdt), D(0)),
                "SOL": Balance("SOL", D(0), D(0)),
                "LINK": Balance("LINK", D(0), D(0)),
            }
        )
    )
    notifier = SimpleNamespace(send=AsyncMock(return_value=1))
    repo = Repo(make_engine(None))
    engine = SignalEngine(
        strategy=load_strategy(),
        snapshots=snapshots,
        repo=repo,
        balances=balances,
        filters=FILTERS,
        notifier=notifier,
        keyboard=lambda signal_id, has_suggestion: ("kb", signal_id, has_suggestion),
        clock=clock or Clock(),
        delay=0,
    )
    return engine, repo, notifier


async def test_fire_saves_signal_and_sends_alert():
    engine, repo, notifier = make_engine_under_test()
    signal = await engine.evaluate_symbol("SOLUSDT")
    assert signal is not None
    saved = repo.get_signal(signal.id)
    assert saved.alert_sent and saved.user_action == "none" and not saved.high_risk
    suggestion = json.loads(saved.suggestion_json)
    # total 200 USDT: risk 3 / 4% stop = 75 -> capped at 30% = 60
    assert D(suggestion["size_usdt"]) == D("60") and suggestion["capped_by"] == "max_position_pct"
    assert [c["name"] for c in json.loads(saved.rule_values_json)][:2] == [
        "drop_from_high",
        "rsi_1h",
    ]
    text = notifier.send.await_args.args[0]
    assert "Dip signal: SOL" in text and "Size: <b>60.00 USDT</b>" in text
    assert notifier.send.await_args.kwargs["reply_markup"] == ("kb", signal.id, True)


async def test_no_fire_no_signal():
    engine, repo, notifier = make_engine_under_test(snapshot(drop=1.0))
    assert await engine.evaluate_symbol("SOLUSDT") is None
    assert repo.last_signal_time("SOLUSDT") is None
    notifier.send.assert_not_awaited()


async def test_cooldown_blocks_repeat_then_allows():
    clock = Clock()
    engine, repo, notifier = make_engine_under_test(clock=clock)
    assert await engine.evaluate_symbol("SOLUSDT") is not None
    clock.t += 60 * 60  # 1h later, cooldown is 180 min
    assert await engine.evaluate_symbol("SOLUSDT") is None
    clock.t += 2 * 60 * 60 + 1
    assert await engine.evaluate_symbol("SOLUSDT") is not None
    assert notifier.send.await_count == 2


async def test_paused_saves_but_does_not_send():
    engine, repo, notifier = make_engine_under_test()
    set_alerts_paused(repo, True)
    signal = await engine.evaluate_symbol("SOLUSDT")
    assert signal is not None and not repo.get_signal(signal.id).alert_sent
    notifier.send.assert_not_awaited()


async def test_high_risk_reaches_message():
    engine, repo, notifier = make_engine_under_test(snapshot(btc_1h=-4.0))
    signal = await engine.evaluate_symbol("SOLUSDT")
    assert repo.get_signal(signal.id).high_risk
    assert "HIGH RISK" in notifier.send.await_args.args[0]


async def test_too_small_sends_reduced_keyboard():
    engine, _, notifier = make_engine_under_test(usdt="20")  # 30% of 20 = 6 < 10 minimum
    signal = await engine.evaluate_symbol("SOLUSDT")
    assert "No trade suggested" in notifier.send.await_args.args[0]
    assert notifier.send.await_args.kwargs["reply_markup"] == ("kb", signal.id, False)


async def test_balance_failure_still_alerts_without_sizing():
    engine, repo, notifier = make_engine_under_test()
    engine.balances.get_balances = AsyncMock(side_effect=ConnectionError("down"))
    signal = await engine.evaluate_symbol("SOLUSDT")
    assert repo.get_signal(signal.id).suggestion_json is None
    assert "Sizing unavailable" in notifier.send.await_args.args[0]


async def test_only_15m_closes_of_configured_symbols_trigger():
    engine, _, _ = make_engine_under_test()
    engine.evaluate_symbol = AsyncMock()
    engine.on_candle_closed("SOLUSDT", "1h")
    engine.on_candle_closed("BTCUSDT", "15m")
    engine.on_candle_closed("SOLUSDT", "15m")
    await asyncio.sleep(0.01)
    engine.evaluate_symbol.assert_awaited_once_with("SOLUSDT")
