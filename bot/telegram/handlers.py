"""Telegram commands, chat-ID allowlist and error handling."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from bot.exchange.client import BinanceClient
from bot.exchange.models import QUOTE_ASSET
from bot.exchange.streams import PriceStream
from bot.settings import Settings
from bot.telegram.messages import (
    COMMANDS,
    balance_message,
    base_asset,
    price_message,
    start_message,
    with_mode,
)
from bot.telegram.notifier import Notifier

log = logging.getLogger(__name__)

ERROR_NOTICE_INTERVAL = 300


@dataclass
class Deps:
    client: BinanceClient
    stream: PriceStream
    settings: Settings
    notifier: Notifier | None = None
    last_error_notice: float = float("-inf")

    @property
    def mode(self) -> str:
        return self.settings.strategy.mode

    @property
    def assets(self) -> list[str]:
        return [QUOTE_ASSET] + [base_asset(s) for s in self.settings.strategy.symbols]


def _deps(context: ContextTypes.DEFAULT_TYPE) -> Deps:
    return context.bot_data["deps"]


async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    await update.effective_message.reply_text(
        with_mode(text, _deps(context).mode), parse_mode=ParseMode.HTML
    )


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every other handler. Silently drops updates from non-allowlisted chats."""
    allowed = _deps(context).settings.secrets.telegram_allowed_chat_ids
    chat = update.effective_chat
    if chat is not None and chat.id in allowed:
        return
    user = update.effective_user
    log.warning(
        "Ignored update from chat_id=%s user=%s",
        chat.id if chat else None,
        f"@{user.username}" if user and user.username else (user.id if user else None),
    )
    raise ApplicationHandlerStop


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    await _reply(update, context, start_message(deps.mode, deps.stream.symbols))


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stream = _deps(context).stream
    await _reply(update, context, price_message(stream.symbols, stream.ticks))


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    try:
        balances = await deps.client.get_balances(deps.assets)
    except Exception:
        log.exception("/balance failed")
        await _reply(update, context, "Couldn't fetch balances right now. Try again shortly.")
        return
    prices = {s: t.price for s, t in deps.stream.ticks.items()}
    await _reply(update, context, balance_message(balances, prices))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error in Telegram handler", exc_info=context.error)
    deps: Deps | None = context.bot_data.get("deps")
    if deps is None or deps.notifier is None:
        return
    now = time.monotonic()
    if now - deps.last_error_notice < ERROR_NOTICE_INTERVAL:
        return
    deps.last_error_notice = now
    await deps.notifier.send(f"⚠️ Error: {type(context.error).__name__}")


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand(name, desc) for name, desc in COMMANDS])


def build_application(token: str, deps: Deps) -> Application:
    app = Application.builder().token(token).post_init(_post_init).build()
    app.bot_data["deps"] = deps
    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_error_handler(error_handler)
    return app

