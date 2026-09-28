"""Entry-alert buttons, custom-amount conversation and /pause, /resume (requirements.md §5.7).

Callback data:
  act:<signal_id>:enter|custom|ignore   buttons on the alert
  cfm:<signal_id>:<usdt>:<price>        "Yes" on the confirmation
  cxl:<signal_id>                       "No" on the confirmation
"""

from __future__ import annotations

import json
import logging
import time
import warnings
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from bot.exchange.models import QUOTE_ASSET
from bot.positions import service
from bot.signals.engine import alerts_paused, set_alerts_paused
from bot.signals.sizing import Suggestion
from bot.storage.models import Signal
from bot.telegram import positions
from bot.telegram.deps import Deps, get_deps, minimum_order, reply
from bot.telegram.messages import base_asset, fmt_price, plain, with_mode

log = logging.getLogger(__name__)

SIGNAL_EXPIRY_SECONDS = 60 * 60
CUSTOM_AMOUNT_TIMEOUT_SECONDS = 5 * 60
AWAITING_AMOUNT = 1
ACTION_PATTERN = r"^act:\d+:(enter|custom|ignore)$"


def entry_keyboard(signal_id: int, has_suggestion: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if has_suggestion:
        enter = InlineKeyboardButton(
            "Enter with suggested amount", callback_data=f"act:{signal_id}:enter"
        )
        rows.append([enter])
    rows.append(
        [
            InlineKeyboardButton("Custom amount", callback_data=f"act:{signal_id}:custom"),
            InlineKeyboardButton("Ignore", callback_data=f"act:{signal_id}:ignore"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def confirm_keyboard(signal_id: int, usdt: Decimal, price: Decimal) -> InlineKeyboardMarkup:
    usdt, price = plain(usdt.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)), plain(price)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Yes", callback_data=f"cfm:{signal_id}:{usdt}:{price}"),
                InlineKeyboardButton("No", callback_data=f"cxl:{signal_id}"),
            ]
        ]
    )


# --- helpers ---------------------------------------------------------------------------


def _check_signal(deps: Deps, signal_id: int, now: float) -> tuple[Signal | None, str | None]:
    signal = deps.repo.get_signal(signal_id) if deps.repo else None
    if signal is None:
        return None, "Signal not found."
    if signal.user_action != "none":
        return signal, f"Already recorded as {signal.user_action}."
    if now - signal.ts > SIGNAL_EXPIRY_SECONDS:
        return signal, "Signal expired (older than 60 minutes). Prices have moved on."
    return signal, None


def _live_price(deps: Deps, signal: Signal) -> Decimal:
    tick = deps.stream.ticks.get(signal.symbol)
    return tick.price if tick is not None else Decimal(str(signal.price))


def _confirm_text(signal: Signal, usdt: Decimal, price: Decimal) -> str:
    return (
        f"Record {base_asset(signal.symbol)} entry of <b>{usdt:,.2f} USDT</b>"
        f" at {fmt_price(price)}?"
    )


async def _answer(update: Update, text: str | None = None) -> None:
    await update.callback_query.answer(text)


async def _append_status(update: Update, deps: Deps, status: str) -> None:
    """Replace the alert's buttons with a status line."""
    message = update.callback_query.message
    try:
        await message.edit_text(
            f"{message.text_html}\n\n{status}", parse_mode=ParseMode.HTML, reply_markup=None
        )
    except Exception as exc:  # message too old / unchanged: not worth failing over
        log.debug("Could not edit alert message: %s", exc)


# --- alert buttons -----------------------------------------------------------------------


async def on_alert_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    deps = get_deps(context)
    _, raw_id, action = update.callback_query.data.split(":")
    signal, problem = _check_signal(deps, int(raw_id), time.time())
    if problem:
        await _answer(update, problem)
        return ConversationHandler.END

    if action == "ignore":
        if deps.repo.set_user_action(signal.id, "ignored"):
            await _answer(update, "Ignored")
            await _append_status(update, deps, "🚫 Ignored")
        else:
            await _answer(update, "Already handled.")
        return ConversationHandler.END

    if action == "enter":
        if not signal.suggestion_json:
            await _answer(update, "No suggested amount for this signal. Use Custom amount.")
            return ConversationHandler.END
        suggestion = Suggestion.model_validate_json(signal.suggestion_json)
        if suggestion.too_small:
            await _answer(update, "No suggested amount for this signal. Use Custom amount.")
            return ConversationHandler.END
        price = _live_price(deps, signal)
        await _answer(update)
        await reply(
            update,
            context,
            _confirm_text(signal, suggestion.size_usdt, price),
            reply_markup=confirm_keyboard(signal.id, suggestion.size_usdt, price),
        )
        return ConversationHandler.END

    # action == "custom"
    await _answer(update)
    context.chat_data["custom"] = {"signal_id": signal.id, "asked_at": time.time()}
    await reply(
        update,
        context,
        f"How many USDT for {base_asset(signal.symbol)}? Send a number, or /cancel.",
    )
    return AWAITING_AMOUNT


async def on_custom_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    deps = get_deps(context)
    pending = context.chat_data.get("custom")
    if not pending or time.time() - pending["asked_at"] > CUSTOM_AMOUNT_TIMEOUT_SECONDS:
        context.chat_data.pop("custom", None)
        await reply(update, context, "That request timed out. Tap Custom amount again.")
        return ConversationHandler.END

    signal, problem = _check_signal(deps, pending["signal_id"], time.time())
    if problem:
        context.chat_data.pop("custom", None)
        await reply(update, context, problem)
        return ConversationHandler.END

    text = (update.effective_message.text or "").strip().replace(",", "")
    try:
        usdt = Decimal(text.removesuffix("USDT").removesuffix("usdt").strip())
    except InvalidOperation:
        await reply(update, context, "Please send a number of USDT, e.g. 25. Or /cancel.")
        return AWAITING_AMOUNT
    if not usdt.is_finite() or usdt <= 0:
        await reply(update, context, "The amount must be a positive number. Or /cancel.")
        return AWAITING_AMOUNT
    usdt = usdt.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)

    minimum = minimum_order(deps, signal.symbol)
    if usdt < minimum:
        await reply(update, context, f"Minimum order is {minimum:.2f} USDT. Try again or /cancel.")
        return AWAITING_AMOUNT
    try:
        balances = await deps.client.get_balances(deps.assets)
        available = balances[QUOTE_ASSET].free
    except Exception:
        log.exception("Balance check for custom amount failed")
        await reply(update, context, "Couldn't check your balance right now. Try again or /cancel.")
        return AWAITING_AMOUNT
    if usdt > available:
        await reply(
            update,
            context,
            f"That's more than your available {available:,.2f} USDT. Try again or /cancel.",
        )
        return AWAITING_AMOUNT

    context.chat_data.pop("custom", None)
    price = _live_price(deps, signal)
    await reply(
        update,
        context,
        _confirm_text(signal, usdt, price),
        reply_markup=confirm_keyboard(signal.id, usdt, price),
    )
    return ConversationHandler.END


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data.pop("custom", None)
    await reply(update, context, "Cancelled.")
    return ConversationHandler.END


# --- confirmation ----------------------------------------------------------------------


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    _, raw_id, raw_usdt, raw_price = update.callback_query.data.split(":")
    signal, problem = _check_signal(deps, int(raw_id), time.time())
    if problem:
        await _answer(update, problem)
        return
    if not deps.repo.set_user_action(signal.id, "entered"):
        await _answer(update, "Already handled.")
        return
    usdt, price = Decimal(raw_usdt), Decimal(raw_price)
    suggested_stop = None
    if signal.suggestion_json:
        suggested_stop = json.loads(signal.suggestion_json).get("stop")
    position = service.open_position(
        deps,
        signal.symbol,
        usdt,
        price,
        stop=Decimal(str(suggested_stop)) if suggested_stop else None,
        signal_id=signal.id,
    )
    paper = " (paper)" if position.is_paper else ""
    stop = f", stop {fmt_price(position.stop_price)}" if position.stop_price else ""
    await _answer(update, "Recorded")
    await update.callback_query.message.edit_text(
        with_mode(
            f"✅ Recorded {base_asset(signal.symbol)} entry #{position.id}{paper}:"
            f" {usdt:,.2f} USDT at {fmt_price(price)}{stop}."
            "\nPlace the order on Binance yourself if you want it for real.",
            deps.mode,
        ),
        parse_mode=ParseMode.HTML,
    )


async def on_cancel_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    await _answer(update, "Cancelled")
    await update.callback_query.message.edit_text(
        with_mode("Cancelled. Nothing recorded.", deps.mode), parse_mode=ParseMode.HTML
    )


# --- /pause, /resume ---------------------------------------------------------------------


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    set_alerts_paused(deps.repo, True)
    await reply(
        update, context, "⏸ Dip alerts paused. Signals are still logged. /resume to turn back on."
    )


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    set_alerts_paused(deps.repo, False)
    await reply(update, context, "▶️ Dip alerts resumed.")


def is_paused(deps: Deps) -> bool:
    return bool(deps.repo) and alerts_paused(deps.repo)


def register(app: Application) -> None:
    with warnings.catch_warnings():
        # We deliberately track the conversation per chat, not per message.
        warnings.simplefilter("ignore", PTBUserWarning)
        conversation = ConversationHandler(
            entry_points=[CallbackQueryHandler(on_alert_action, pattern=ACTION_PATTERN)],
            states={
                AWAITING_AMOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, on_custom_amount),
                    CallbackQueryHandler(on_alert_action, pattern=ACTION_PATTERN),
                ]
            },
            fallbacks=[CommandHandler("cancel", on_cancel)],
            per_chat=True,
            per_user=True,
        )
    app.add_handler(conversation)
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^cfm:\d+:[\d.]+:[\d.]+$"))
    app.add_handler(CallbackQueryHandler(on_cancel_confirm, pattern=r"^cxl:\d+$"))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    positions.register(app)
