from decimal import Decimal as D
from unittest.mock import AsyncMock, MagicMock

from bot.exchange.streams import PriceStream
from bot.main import StaleMonitor, check_stale


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def tick_msg(price):
    return {"data": {"e": "24hrMiniTicker", "E": 1, "s": "SOLUSDT", "c": price, "o": price}}


async def test_stale_alert_once_then_recovery():
    clock = Clock()
    stream = PriceStream(["SOLUSDT"], socket_factory=None, clock=clock)
    notifier = MagicMock()
    notifier.send = AsyncMock()
    monitor = StaleMonitor()

    stream.handle_message(tick_msg("100"))
    await check_stale(stream, monitor, notifier)
    notifier.send.assert_not_awaited()

    clock.now = 301
    await check_stale(stream, monitor, notifier)
    clock.now = 400
    await check_stale(stream, monitor, notifier)
    assert notifier.send.await_count == 1
    assert "No price data" in notifier.send.await_args.args[0]

    stream.handle_message(tick_msg("101"))
    await check_stale(stream, monitor, notifier)
    assert notifier.send.await_count == 2
    assert "recovered" in notifier.send.await_args.args[0]
    assert stream.ticks["SOLUSDT"].price == D("101")
