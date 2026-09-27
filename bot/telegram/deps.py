"""Shared dependencies for Telegram handlers, stored in `app.bot_data["deps"]`."""

from __future__ import annotations

from dataclasses import dataclass, field

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.exchange.client import BinanceClient
from bot.exchange.models import QUOTE_ASSET
from bot.exchange.streams import PriceStream
from bot.market.snapshot import SnapshotBuilder
from bot.settings import Settings
from bot.signals.sizing import SymbolFilters
from bot.storage.db import Repo
from bot.telegram.messages import base_asset, with_mode
from bot.telegram.notifier import Notifier


@dataclass
class Deps:
    client: BinanceClient
    stream: PriceStream
    settings: Settings
    snapshots: SnapshotBuilder | None = None
    repo: Repo | None = None
    filters: dict[str, SymbolFilters] = field(default_factory=dict)
    notifier: Notifier | None = None
    last_error_notice: float = float("-inf")

    @property
    def mode(self) -> str:
        return self.settings.strategy.mode

    @property
    def assets(self) -> list[str]:
        return [QUOTE_ASSET] + [base_asset(s) for s in self.settings.strategy.symbols]


def get_deps(context: ContextTypes.DEFAULT_TYPE) -> Deps:
    return context.bot_data["deps"]


async def reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None
) -> None:
    await update.effective_message.reply_text(
        with_mode(text, get_deps(context).mode),
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )
