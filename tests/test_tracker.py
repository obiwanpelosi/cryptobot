"""Phase 5 "done when": a simulated position triggers each alert exactly once."""

import asyncio
import json
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest

from bot.exchange.models import Tick
from bot.positions.tracker import PositionTracker
from bot.settings import load_strategy
from bot.storage.db import Repo, make_engine
from bot.storage.models import Position


def add_position(repo, **kw):
    fields = dict(
        symbol="SOLUSDT",
        entry_time=0,
        entry_price=100.0,
        amount_usdt=100.0,
        quantity=0.999,
        stop_price=96.0,
        fees_usdt=0.1,
        is_paper=True,
        highest_price=100.0,
    )
    fields.update(kw)
    return repo.add_position(Position(**fields))


def make_tracker(repo, send_results=None, atr=None):
    notifier = AsyncMock()
    notifier.send = (
        AsyncMock(side_effect=send_results) if send_results else AsyncMock(return_value=1)
    )
    tracker = PositionTracker(
        repo=repo,
        strategy=load_strategy(),
        notifier=notifier,
        atr_4h=lambda symbol: atr,
        tick_size=lambda symbol: 0.01,
        clock=lambda: 0.0,
    )
    tracker.load()
    return tracker, notifier


async def feed(tracker, *prices, symbol="SOLUSDT"):
    for price in prices:
        tracker.on_tick(Tick(symbol, D(str(price)), D(0), 0))
        await asyncio.gather(*list(tracker._tasks))


def sent_texts(notifier):
    return [c.args[0] for c in notifier.send.await_args_list]


@pytest.fixture
def repo():
    return Repo(make_engine(None))


async def test_each_alert_exactly_once_and_not_after_restart(repo):
    position = add_position(repo)
    tracker, notifier = make_tracker(repo)

    await feed(tracker, 104)  # +3.8%: nothing
    assert notifier.send.await_count == 0
    await feed(tracker, 110.3, 110.5)  # +10.08%: target 10, once
    assert notifier.send.await_count == 1
    await feed(tracker, 116)  # +15.77%: target 15 + trailing suggestion, one message
    assert notifier.send.await_count == 2
    await feed(tracker, 99)  # nothing
    await feed(tracker, 97.5, 97.6)  # near stop, once
    assert notifier.send.await_count == 3
    await feed(tracker, 95, 94)  # stop hit, once
    assert notifier.send.await_count == 4

    keys = repo.sent_alert_keys([position.id])[position.id]
    assert keys == {"target:10", "target:15", "trail_suggest", "near_stop:96", "stop_hit:96"}
    assert json.loads(repo.get_position(position.id).targets_hit) == [10, 15]

    # "Restart": a fresh tracker on the same database sends nothing more.
    restarted, notifier2 = make_tracker(repo)
    await feed(restarted, 116, 97.5, 95)
    notifier2.send.assert_not_awaited()


async def test_failed_send_retried_then_recorded(repo):
    position = add_position(repo)
    tracker, notifier = make_tracker(repo, send_results=[0, 1])
    await feed(tracker, 110.5)  # send fails (0 delivered): not recorded
    assert repo.sent_alert_keys([position.id])[position.id] == set()
    await feed(tracker, 110.6)  # retried on the next tick
    assert repo.sent_alert_keys([position.id])[position.id] == {"target:10"}
    await feed(tracker, 110.7)
    assert notifier.send.await_count == 2


async def test_no_duplicate_while_send_in_flight(repo):
    add_position(repo)
    tracker, notifier = make_tracker(repo)
    tracker.on_tick(Tick("SOLUSDT", D("110.5"), D(0), 0))
    tracker.on_tick(Tick("SOLUSDT", D("110.6"), D(0), 0))  # before the first send finishes
    await asyncio.gather(*list(tracker._tasks))
    assert notifier.send.await_count == 1


async def test_other_symbols_and_closed_positions_ignored(repo):
    position = add_position(repo)
    tracker, notifier = make_tracker(repo)
    await feed(tracker, 200, symbol="LINKUSDT")
    repo.close_position(position.id, exit_price=100.0)
    tracker.reload()
    await feed(tracker, 200)
    notifier.send.assert_not_awaited()


async def test_trailing_stop_ratchets_and_hit_follows(repo):
    position = add_position(repo, trailing=True)
    tracker, notifier = make_tracker(repo, atr=2.0)  # trail = highest - 4
    await feed(tracker, 116)
    assert tracker.positions[position.id].stop_price == 112
    assert repo.get_position(position.id).stop_price == 112  # persisted when it moves
    await feed(tracker, 114)
    assert tracker.positions[position.id].stop_price == 112  # never down
    await feed(tracker, 120)
    assert tracker.positions[position.id].stop_price == 116
    await feed(tracker, 115.5)
    keys = repo.sent_alert_keys([position.id])[position.id]
    assert "stop_hit:116" in keys and "stop_hit:96" not in keys
    assert "trail_suggest" not in keys  # already trailing
    assert "stop_hit:116" in sent_texts(notifier)[-1]
