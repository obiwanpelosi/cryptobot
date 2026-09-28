"""Shared dependencies for Telegram handlers, stored in `app.bot_data["deps"]`."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from bot.ai.advisor import AIAdvisor
    from bot.ai.models import ModelCatalog
    from bot.positions.tracker import PositionTracker


@dataclass
class Deps:
    client: BinanceClient
    stream: PriceStream
    settings: Settings
    snapshots: SnapshotBuilder | None = None
    repo: Repo | None = None
    filters: dict[str, SymbolFilters] = field(default_factory=dict)
    notifier: Notifier | None = None
    tracker: PositionTracker | None = None
    advisor: AIAdvisor | None = None
    catalog: ModelCatalog | None = None
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
):
    return await update.effective_message.reply_text(
        with_mode(text, get_deps(context).mode),
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


def resolve_symbol(arg: str, symbols: list[str]) -> str | None:
    """Accept SOL, sol or SOLUSDT for a configured symbol."""
    wanted = arg.strip().upper()
    for symbol in symbols:
        if wanted in (symbol, base_asset(symbol)):
            return symbol
    return None


def minimum_order(deps: Deps, symbol: str) -> Decimal:
    configured = Decimal(str(deps.settings.strategy.sizing.min_order_usdt))
    exchange = deps.filters[symbol].min_notional if symbol in deps.filters else Decimal(0)
    return max(configured, exchange)
