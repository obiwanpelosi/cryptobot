"""Telegram commands, chat-ID allowlist and error handling."""

from __future__ import annotations

import logging
import time

from telegram import BotCommand, Update
from telegram.error import Conflict, NetworkError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from bot.telegram import ai as ai_commands
from bot.telegram import alerts
from bot.telegram.deps import Deps, get_deps, reply, resolve_symbol
from bot.telegram.messages import (
    COMMANDS,
    analysis_message,
    balance_message,
    base_asset,
    price_message,
    start_message,
)

log = logging.getLogger(__name__)

ERROR_NOTICE_INTERVAL = 300


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every other handler. Silently drops updates from non-allowlisted chats."""
    allowed = get_deps(context).settings.secrets.telegram_allowed_chat_ids
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
    deps = get_deps(context)
    text = start_message(deps.mode, deps.stream.symbols, paused=alerts.is_paused(deps))
    await reply(update, context, text)


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stream = get_deps(context).stream
    await reply(update, context, price_message(stream.symbols, stream.ticks))


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    try:
        balances = await deps.client.get_balances(deps.assets)
    except Exception:
        log.exception("/balance failed")
        await reply(update, context, "Couldn't fetch balances right now. Try again shortly.")
        return
    prices = {s: t.price for s, t in deps.stream.ticks.items()}
    await reply(update, context, balance_message(balances, prices))


async def analysis_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    symbols = deps.settings.strategy.symbols
    valid = ", ".join(base_asset(s) for s in symbols)
    symbol = resolve_symbol(context.args[0], symbols) if context.args else None
    if symbol is None:
        await reply(update, context, f"Usage: /analysis &lt;SYMBOL&gt;. One of: {valid}")
        return
    snapshot = deps.snapshots.build(symbol) if deps.snapshots else None
    if snapshot is None:
        await reply(update, context, "Still warming up (loading candles). Try again shortly.")
        return
    text = analysis_message(snapshot)
    message = await reply(update, context, text)
    if deps.advisor is not None and message is not None:
        await _append_ai_view(deps, message, text, snapshot)


async def _append_ai_view(deps: Deps, message, text: str, snapshot) -> None:
    """/analysis 'AI view if enabled' (spec §5.7), using the light model."""
    from telegram.constants import ParseMode

    from bot.ai.prompts import entry_context, trade_history
    from bot.telegram.messages import ai_entry_section, ai_skipped_line, with_mode

    try:
        history = trade_history(deps.repo.recent_closed_positions(snapshot.symbol, 20))
        ctx = entry_context(
            snapshot=snapshot,
            rule_result=None,
            suggestion=None,
            portfolio={},
            history=history,
            purpose="analysis",
        )
        outcome = await deps.advisor.analyze_entry(ctx, purpose="analysis", role="light")
        if outcome is None:
            reason = deps.advisor.last_skip_reason or "no usable answer"
            section = ai_skipped_line(reason)
        else:
            advice, model = outcome
            section = ai_entry_section(advice, model, None)
        await message.edit_text(
            with_mode(f"{text}\n\n{section}", deps.mode), parse_mode=ParseMode.HTML
        )
    except Exception:
        log.exception("AI view for /analysis failed")


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


_last_polling_warning: dict[str, float] = {}


def polling_error(exc: Exception) -> None:
    """Error callback for getUpdates polling: one readable line instead of a traceback."""
    if isinstance(exc, Conflict):
        key, text = (
            "conflict",
            (
                "Telegram Conflict: another instance of this bot is already running with the "
                "same token. Stop the other one (only one poller is allowed)."
            ),
        )
    elif isinstance(exc, NetworkError):
        key, text = "network", f"Telegram polling network error: {exc}"
    else:
        log.error("Telegram polling error", exc_info=exc)
        return
    now = time.monotonic()
    if now - _last_polling_warning.get(key, float("-inf")) >= 60:
        _last_polling_warning[key] = now
        log.warning(text)


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand(name, desc) for name, desc in COMMANDS])


def build_application(token: str, deps: Deps) -> Application:
    app = Application.builder().token(token).post_init(_post_init).build()
    app.bot_data["deps"] = deps
    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("analysis", analysis_cmd))
    alerts.register(app)
    ai_commands.register(app)
    app.add_error_handler(error_handler)
    return app
