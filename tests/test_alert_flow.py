import time
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram.ext import ConversationHandler

from bot.exchange.models import Balance, Tick
from bot.settings import Secrets, Settings, load_strategy
from bot.signals.sizing import Suggestion, SymbolFilters
from bot.storage.db import Repo, make_engine
from bot.storage.models import Signal
from bot.telegram import alerts
from bot.telegram.deps import Deps

FILTERS = {"SOLUSDT": SymbolFilters(tick_size=D("0.01"), step_size=D("0.001"), min_notional=D("5"))}
SUGGESTION = Suggestion(
    entry=D("100"), stop=D("96"), stop_distance_pct=D("4"), size_usdt=D("60"), qty=D("0.6")
)


def make_deps(usdt="200"):
    settings = Settings(secrets=Secrets(_env_file=None), strategy=load_strategy())
    client = MagicMock()
    client.get_balances = AsyncMock(
        return_value={"USDT": Balance("USDT", D(usdt), D(0)), "SOL": Balance("SOL", D(0), D(0))}
    )
    stream = SimpleNamespace(ticks={"SOLUSDT": Tick("SOLUSDT", D("101.5"), D("0"), 0)})
    return Deps(
        client=client,
        stream=stream,
        settings=settings,
        repo=Repo(make_engine(None)),
        filters=FILTERS,
    )


def add_signal(deps, age=0, suggestion=SUGGESTION):
    return deps.repo.add_signal(
        Signal(
            symbol="SOLUSDT",
            ts=int(time.time()) - age,
            price=100.0,
            snapshot_json="{}",
            rule_values_json="[]",
            high_risk=False,
            suggestion_json=suggestion.model_dump_json() if suggestion else None,
        )
    )


def callback_update(data):
    message = SimpleNamespace(
        text_html="[PAPER] alert", edit_text=AsyncMock(), reply_text=AsyncMock()
    )
    query = SimpleNamespace(data=data, answer=AsyncMock(), message=message)
    return SimpleNamespace(callback_query=query, effective_message=message)


def text_update(text):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(effective_message=message)


def ctx(deps, chat_data=None):
    return SimpleNamespace(
        bot_data={"deps": deps}, chat_data=chat_data if chat_data is not None else {}
    )


def last_reply(update):
    return update.effective_message.reply_text.await_args


# --- keyboards ---------------------------------------------------------------------------


def test_keyboards_and_callback_data_size():
    kb = alerts.entry_keyboard(7, True).inline_keyboard
    assert [b.callback_data for row in kb for b in row] == [
        "act:7:enter",
        "act:7:custom",
        "act:7:ignore",
    ]
    reduced = alerts.entry_keyboard(7, False).inline_keyboard
    assert [b.callback_data for row in reduced for b in row] == ["act:7:custom", "act:7:ignore"]
    cfm = alerts.confirm_keyboard(123456, D("58.40109"), D("1.2E+2")).inline_keyboard[0][0]
    assert cfm.callback_data == "cfm:123456:58.4:120"
    assert len(cfm.callback_data.encode()) <= 64


# --- enter -> confirm -> recorded ------------------------------------------------------


async def test_enter_then_yes_records_position():
    deps = make_deps()
    signal = add_signal(deps)
    update = callback_update(f"act:{signal.id}:enter")
    await alerts.on_alert_action(update, ctx(deps))
    call = last_reply(update)
    assert "Record SOL entry of <b>60.00 USDT</b> at 101.50?" in call.args[0]
    yes = call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    assert yes == f"cfm:{signal.id}:60:101.5"

    confirm = callback_update(yes)
    await alerts.on_confirm(confirm, ctx(deps))
    (position,) = deps.repo.open_positions()
    assert position.amount_usdt == 60 and position.entry_price == 101.5
    assert (
        position.stop_price == 96 and position.is_paper and position.linked_signal_id == signal.id
    )
    # qty = 60/101.5*0.999 = 0.59054... -> rounded down to step 0.001
    assert position.quantity == 0.590
    assert position.fees_usdt == 0.06
    assert deps.repo.get_signal(signal.id).user_action == "entered"
    assert "✅ Recorded SOL entry" in confirm.callback_query.message.edit_text.await_args.args[0]


async def test_double_confirm_is_rejected():
    deps = make_deps()
    signal = add_signal(deps)
    await alerts.on_confirm(callback_update(f"cfm:{signal.id}:60:101.5"), ctx(deps))
    again = callback_update(f"cfm:{signal.id}:60:101.5")
    await alerts.on_confirm(again, ctx(deps))
    assert len(deps.repo.open_positions()) == 1
    assert "Already recorded" in again.callback_query.answer.await_args.args[0]


async def test_ignore_and_tap_again():
    deps = make_deps()
    signal = add_signal(deps)
    update = callback_update(f"act:{signal.id}:ignore")
    await alerts.on_alert_action(update, ctx(deps))
    assert deps.repo.get_signal(signal.id).user_action == "ignored"
    assert "Ignored" in update.callback_query.message.edit_text.await_args.args[0]
    again = callback_update(f"act:{signal.id}:enter")
    await alerts.on_alert_action(again, ctx(deps))
    assert "Already recorded as ignored" in again.callback_query.answer.await_args.args[0]


async def test_expired_signal_refused():
    deps = make_deps()
    signal = add_signal(deps, age=alerts.SIGNAL_EXPIRY_SECONDS + 5)
    update = callback_update(f"act:{signal.id}:enter")
    await alerts.on_alert_action(update, ctx(deps))
    assert "expired" in update.callback_query.answer.await_args.args[0]
    update.effective_message.reply_text.assert_not_awaited()


async def test_enter_without_suggestion_points_to_custom():
    deps = make_deps()
    signal = add_signal(deps, suggestion=None)
    update = callback_update(f"act:{signal.id}:enter")
    await alerts.on_alert_action(update, ctx(deps))
    assert "Custom amount" in update.callback_query.answer.await_args.args[0]


async def test_no_cancels_without_recording():
    deps = make_deps()
    signal = add_signal(deps)
    update = callback_update(f"cxl:{signal.id}")
    await alerts.on_cancel_confirm(update, ctx(deps))
    assert deps.repo.open_positions() == []
    assert deps.repo.get_signal(signal.id).user_action == "none"


# --- custom amount -----------------------------------------------------------------------


async def test_custom_amount_validation_then_confirm():
    deps = make_deps(usdt="200")
    signal = add_signal(deps)
    chat_data = {}
    start = callback_update(f"act:{signal.id}:custom")
    assert await alerts.on_alert_action(start, ctx(deps, chat_data)) == alerts.AWAITING_AMOUNT
    assert "How many USDT" in last_reply(start).args[0]

    for bad, expected in (
        ("abc", "Please send a number"),
        ("-5", "positive number"),
        ("5000", "more than your available 200.00 USDT"),
        ("3", "Minimum order is 10.00 USDT"),
    ):
        update = text_update(bad)
        assert await alerts.on_custom_amount(update, ctx(deps, chat_data)) == alerts.AWAITING_AMOUNT
        assert expected in last_reply(update).args[0], bad

    good = text_update("25 usdt")
    assert await alerts.on_custom_amount(good, ctx(deps, chat_data)) == ConversationHandler.END
    call = last_reply(good)
    assert "Record SOL entry of <b>25.00 USDT</b>" in call.args[0]
    assert (
        call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        == f"cfm:{signal.id}:25:101.5"
    )
    assert "custom" not in chat_data


async def test_custom_amount_timeout():
    deps = make_deps()
    signal = add_signal(deps)
    chat_data = {"custom": {"signal_id": signal.id, "asked_at": time.time() - 10_000}}
    update = text_update("25")
    assert await alerts.on_custom_amount(update, ctx(deps, chat_data)) == ConversationHandler.END
    assert "timed out" in last_reply(update).args[0]


async def test_cancel_clears_pending():
    deps = make_deps()
    chat_data = {"custom": {"signal_id": 1, "asked_at": time.time()}}
    update = text_update("/cancel")
    assert await alerts.on_cancel(update, ctx(deps, chat_data)) == ConversationHandler.END
    assert chat_data == {}


# --- pause / resume ----------------------------------------------------------------------


async def test_pause_resume_persist():
    deps = make_deps()
    await alerts.pause_cmd(text_update("/pause"), ctx(deps))
    assert alerts.is_paused(deps)
    await alerts.resume_cmd(text_update("/resume"), ctx(deps))
    assert not alerts.is_paused(deps)
