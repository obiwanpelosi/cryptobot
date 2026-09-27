"""Outbound Telegram messages to the allowed chats. Never raises."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from telegram.constants import ParseMode

from bot.telegram.messages import with_mode

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot: Any, chat_ids: Iterable[int], mode: str):
        self._bot = bot
        self._chat_ids = list(chat_ids)
        self._mode = mode

    async def send(self, text: str) -> None:
        for chat_id in self._chat_ids:
            try:
                await self._bot.send_message(
                    chat_id=chat_id, text=with_mode(text, self._mode), parse_mode=ParseMode.HTML
                )
            except Exception as exc:
                log.warning("Telegram send to %s failed: %s", chat_id, exc)


class NullNotifier:
    """Used when no Telegram token is configured."""

    async def send(self, text: str) -> None:
        return None
