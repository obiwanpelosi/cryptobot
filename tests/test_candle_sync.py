import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from bot.main import CandleSync


async def run_backfill(advanced):
    store = SimpleNamespace(backfill=AsyncMock(return_value=advanced))
    sync = CandleSync(store, client=object())
    sync.engine = MagicMock()
    sync.schedule_backfill()
    await sync._task
    return sync.engine


async def test_backfilled_close_triggers_engine_once_per_key():
    engine = await run_backfill({("SOLUSDT", "15m"), ("SOLUSDT", "1h")})
    calls = [c.args for c in engine.on_candle_closed.call_args_list]
    assert sorted(calls) == [("SOLUSDT", "15m"), ("SOLUSDT", "1h")]


async def test_unchanged_backfill_triggers_nothing():
    engine = await run_backfill(set())
    engine.on_candle_closed.assert_not_called()


async def test_reconnect_schedules_backfill_only_after_first_connect():
    store = SimpleNamespace(backfill=AsyncMock(return_value=set()))
    sync = CandleSync(store, client=object())
    sync.on_connect()
    await asyncio.sleep(0)
    store.backfill.assert_not_awaited()
    sync.on_connect()
    await sync._task
    store.backfill.assert_awaited_once()
