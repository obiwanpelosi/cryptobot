"""/positions, /enter, /close, /history and the buttons on position alerts (§5.7, §5.8).

Callback data:
  ent:<symbol>:<usdt>:<price>   "Yes" on an /enter confirmation
  cls:<position_id>:<price>     "Yes" on a close confirmation
  no:<what>                     "No" on either confirmation
  pos:<position_id>:<action>    buttons on position alerts: tp | hold | trail | close | keep
"""

from __future__ import annotations

import logging
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from bot.positions import service
from bot.positions.logic import next_target, position_pnl
from bot.signals.sizing import fee_fraction
from bot.telegram.deps import Deps, get_deps, minimum_order, reply, resolve_symbol
from bot.telegram.messages import (
    base_asset,
    fmt_pct,
    fmt_price,
    fmt_usdt,
    history_message,
    plain,
    position_alert_text,
    positions_message,
    with_mode,
)

log = logging.getLogger(__name__)


def _live_price(deps: Deps, symbol: str) -> Decimal | None:
    tick = deps.stream.ticks.get(symbol)
    return tick.price if tick is not None else None


def _parse_decimal(text: str) -> Decimal | None:
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def _yes_no(yes_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Yes", callback_data=yes_data),
                InlineKeyboardButton("No", callback_data="no:x"),
            ]
        ]
    )


def _once(context: ContextTypes.DEFAULT_TYPE, update: Update) -> bool:
    """True the first time a confirmation message's Yes is used; False on repeat taps."""
    used = context.chat_data.setdefault("used_confirmations", set())
    key = update.callback_query.message.message_id
    if key in used:
        return False
    used.add(key)
    return True


async def _edit(update: Update, deps: Deps, text: str) -> None:
    await update.callback_query.message.edit_text(
        with_mode(text, deps.mode), parse_mode=ParseMode.HTML, reply_markup=None
    )


# --- /positions, /history ------------------------------------------------------------------


def position_rows(deps: Deps) -> list[dict]:
    fee = float(fee_fraction(deps.settings.strategy.fees))
    targets = deps.settings.strategy.exit_alerts.profit_targets_pct
    rows = []
    for p in sorted(deps.repo.open_positions(), key=lambda p: p.id):
        price = _live_price(deps, p.symbol)
        row = {"position": p, "price": None, "pnl_usdt": 0.0, "pnl_pct": 0.0, "next_target": None}
        if price is not None:
            pnl_usdt, pnl_pct = position_pnl(p.amount_usdt, p.quantity, float(price), fee)
            row.update(
                price=float(price),
                pnl_usdt=pnl_usdt,
                pnl_pct=pnl_pct,
                next_target=next_target(pnl_pct, targets),
            )
        else:
            row["next_target"] = next_target(0.0, targets)
        rows.append(row)
    return rows


async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, context, positions_message(position_rows(get_deps(context))))


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    await reply(update, context, history_message(deps.repo.closed_positions(20)))


# --- /enter --------------------------------------------------------------------------------

ENTER_USAGE = "Usage: /enter &lt;SYMBOL&gt; &lt;USDT&gt; [price], e.g. /enter SOL 50"


async def enter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    args = context.args or []
    symbols = deps.settings.strategy.symbols
    if len(args) not in (2, 3):
        await reply(update, context, ENTER_USAGE)
        return
    symbol = resolve_symbol(args[0], symbols)
    if symbol is None:
        valid = ", ".join(base_asset(s) for s in symbols)
        await reply(update, context, f"Unknown symbol. One of: {valid}")
        return
    usdt = _parse_decimal(args[1])
    if usdt is None:
        await reply(update, context, f"The USDT amount must be a positive number. {ENTER_USAGE}")
        return
    usdt = usdt.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    minimum = minimum_order(deps, symbol)
    if usdt < minimum:
        await reply(update, context, f"Minimum order is {minimum:.2f} USDT.")
        return
    if len(args) == 3:
        price = _parse_decimal(args[2])
        if price is None:
            await reply(update, context, f"The price must be a positive number. {ENTER_USAGE}")
            return
    else:
        price = _live_price(deps, symbol)
        if price is None:
            await reply(update, context, "No live price yet. Give a price: /enter SOL 50 121.5")
            return

    stop = service.default_stop(deps, symbol, price)
    stop_text = f", stop {fmt_price(stop)}" if stop else ""
    await reply(
        update,
        context,
        f"Record {base_asset(symbol)} entry of <b>{usdt:,.2f} USDT</b> at {fmt_price(price)}"
        f"{stop_text}?",
        reply_markup=_yes_no(f"ent:{symbol}:{plain(usdt)}:{plain(price)}"),
    )


async def on_enter_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    _, symbol, raw_usdt, raw_price = update.callback_query.data.split(":")
    if not _once(context, update):
        await update.callback_query.answer("Already recorded.")
        return
    await update.callback_query.answer("Recorded")
    position = service.open_position(deps, symbol, Decimal(raw_usdt), Decimal(raw_price))
    paper = " (paper)" if position.is_paper else ""
    stop = f", stop {fmt_price(position.stop_price)}" if position.stop_price else ""
    await _edit(
        update,
        deps,
        f"✅ Recorded {base_asset(symbol)} entry #{position.id}{paper}:"
        f" {position.amount_usdt:,.2f} USDT at {fmt_price(position.entry_price)}{stop}.",
    )


# --- /close --------------------------------------------------------------------------------

CLOSE_USAGE = "Usage: /close &lt;ID&gt; [price], e.g. /close 3. See /positions for IDs."


async def _ask_close(update: Update, context, deps: Deps, position_id: int, price) -> None:
    position = deps.repo.get_position(position_id)
    if position is None:
        await reply(update, context, f"No position #{position_id}. See /positions.")
        return
    if position.status != "open":
        await reply(update, context, f"Position #{position_id} is already closed.")
        return
    if price is None:
        price = _live_price(deps, position.symbol)
        if price is None:
            await reply(update, context, f"No live price yet. Give one: /close {position_id} 121.5")
            return
    r = service.preview_close(deps, position, float(price))
    await reply(
        update,
        context,
        f"Close #{position.id} {base_asset(position.symbol)} at {fmt_price(price)}?"
        f" P/L {fmt_usdt(r['realised_pnl_usdt'])} ({fmt_pct(r['realised_pnl_pct'])})"
        " after fees.",
        reply_markup=_yes_no(f"cls:{position.id}:{plain(Decimal(str(price)))}"),
    )


async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    args = context.args or []
    if len(args) not in (1, 2) or not args[0].lstrip("#").isdigit():
        await reply(update, context, CLOSE_USAGE)
        return
    price = None
    if len(args) == 2:
        price = _parse_decimal(args[1])
        if price is None:
            await reply(update, context, f"The price must be a positive number. {CLOSE_USAGE}")
            return
    await _ask_close(update, context, deps, int(args[0].lstrip("#")), price)


async def on_close_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    _, raw_id, raw_price = update.callback_query.data.split(":")
    closed = service.close_position(deps, int(raw_id), float(raw_price))
    if closed is None:
        await update.callback_query.answer("Already closed.")
        return
    await update.callback_query.answer("Closed")
    mark = "🟢" if (closed.realised_pnl_usdt or 0) > 0 else "🔴"
    await _edit(
        update,
        deps,
        f"{mark} Closed #{closed.id} {base_asset(closed.symbol)} at {fmt_price(closed.exit_price)}:"
        f" P/L {fmt_usdt(closed.realised_pnl_usdt)} ({fmt_pct(closed.realised_pnl_pct)})"
        " after fees.\nSell on Binance yourself if you haven't already.",
    )


async def on_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer("Cancelled")
    await _edit(update, get_deps(context), "Cancelled. Nothing changed.")


# --- position alert buttons ----------------------------------------------------------------


def alert_keyboard(position_id: int, kinds: set[str], trailing: bool) -> InlineKeyboardMarkup:
    if kinds & {"stop_hit", "near_stop"}:
        rows = [
            [
                InlineKeyboardButton("Close now", callback_data=f"pos:{position_id}:close"),
                InlineKeyboardButton("Keep open", callback_data=f"pos:{position_id}:keep"),
            ]
        ]
    else:
        rows = [
            [
                InlineKeyboardButton("Take profit", callback_data=f"pos:{position_id}:tp"),
                InlineKeyboardButton("Hold", callback_data=f"pos:{position_id}:hold"),
            ]
        ]
        if not trailing:
            rows.append(
                [
                    InlineKeyboardButton(
                        "Set trailing stop", callback_data=f"pos:{position_id}:trail"
                    )
                ]
            )
    return InlineKeyboardMarkup(rows)


def render_position_alert(**ctx) -> tuple[str, InlineKeyboardMarkup]:
    position = ctx["position"]
    kinds = {a.kind for a in ctx["due"]}
    return position_alert_text(**ctx), alert_keyboard(position.id, kinds, bool(position.trailing))


async def on_position_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    _, raw_id, action = update.callback_query.data.split(":")
    position = deps.repo.get_position(int(raw_id))
    if position is None or position.status != "open":
        await update.callback_query.answer("This position is already closed.")
        return
    message = update.callback_query.message

    if action in ("hold", "keep"):
        await update.callback_query.answer("Holding")
        await message.edit_text(
            f"{message.text_html}\n\n✋ Holding", parse_mode=ParseMode.HTML, reply_markup=None
        )
        return

    if action in ("tp", "close"):
        await update.callback_query.answer()
        await _ask_close(update, context, deps, position.id, None)
        return

    # action == "trail"
    if position.trailing:
        await update.callback_query.answer("Trailing stop is already on.")
        return
    tracker = deps.tracker
    new_stop = tracker.trailing_stop_for(position) if tracker else position.stop_price
    deps.repo.update_position(position.id, trailing=True, stop_price=new_stop)
    if tracker:
        tracker.reload()
    await update.callback_query.answer("Trailing stop on")
    await reply(
        update,
        context,
        f"📈 Trailing stop on for #{position.id} {base_asset(position.symbol)}:"
        f" stop now {fmt_price(new_stop)}. It moves up with new highs and never down."
        " Update your stop on Binance too if you use one.",
    )


def register(app: Application) -> None:
    app.add_handler(CommandHandler("positions", positions_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("enter", enter_cmd))
    app.add_handler(CommandHandler("close", close_cmd))
    app.add_handler(
        CallbackQueryHandler(on_enter_confirm, pattern=r"^ent:[A-Z0-9]+:[\d.]+:[\d.]+$")
    )
    app.add_handler(CallbackQueryHandler(on_close_confirm, pattern=r"^cls:\d+:[\d.]+$"))
    app.add_handler(CallbackQueryHandler(on_no, pattern=r"^no:"))
    app.add_handler(
        CallbackQueryHandler(on_position_action, pattern=r"^pos:\d+:(tp|hold|trail|close|keep)$")
    )
