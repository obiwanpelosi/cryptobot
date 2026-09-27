import logging
from unittest.mock import AsyncMock, MagicMock

from bot.telegram.notifier import Notifier


async def test_sends_to_every_chat_with_mode_prefix():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    await Notifier(bot, [1, 2], "paper").send("hello")
    assert bot.send_message.await_count == 2
    sent = [c.kwargs for c in bot.send_message.await_args_list]
    assert [s["chat_id"] for s in sent] == [1, 2]
    assert all(s["text"] == "[PAPER] hello" for s in sent)


async def test_failure_logged_not_raised(caplog):
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[ConnectionError("offline"), None])
    with caplog.at_level(logging.WARNING):
        await Notifier(bot, [1, 2], "live-advisory").send("hi")
    assert "send to 1 failed" in caplog.text
    assert bot.send_message.await_count == 2
