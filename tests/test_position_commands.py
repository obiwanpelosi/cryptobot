from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.exchange.models import Tick
from bot.positions import service
from bot.positions.logic import DueAlert
from bot.positions.tracker import PositionTracker
from bot.settings import Secrets, Settings, load_strategy
from bot.signals.sizing import SymbolFilters
from bot.storage.db import Repo, make_engine
from bot.telegram import positions as pos_handlers
from bot.telegram.deps import Deps
from tests.test_tracker import add_position

FILTERS = {"SOLUSDT": SymbolFilters(tick_size=D("0.01"), step_size=D("0.001"), min_notional=D("5"))}


def make_deps(price="121.5", atr=2.2):
    settings = Settings(secrets=Secrets(_env_file=None), strategy=load_strategy())
    repo = Repo(make_engine(None))
    stream = SimpleNamespace(ticks={"SOLUSDT": Tick("SOLUSDT", D(price), D(0), 0)})
    snapshots = SimpleNamespace(base_indicators=lambda s, tf: SimpleNamespace(atr=atr))
    deps = Deps(
        client=MagicMock(),
        stream=stream,
        settings=settings,
        snapshots=snapshots,
        repo=repo,
        filters=FILTERS,
    )
    deps.tracker = PositionTracker(
        repo=repo,
        strategy=settings.strategy,
        notifier=AsyncMock(),
        atr_4h=lambda s: atr,
        tick_size=lambda s: 0.01,
    )
    deps.tracker.load()
    return deps


def cmd_update():
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(effective_message=message)


def cb_update(data, message_id=1):
    message = SimpleNamespace(
        message_id=message_id,
        text_html="[PAPER] alert",
        edit_text=AsyncMock(),
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        callback_query=SimpleNamespace(data=data, answer=AsyncMock(), message=message),
        effective_message=message,
    )


def ctx(deps, args=None, chat_data=None):
    return SimpleNamespace(
        bot_data={"deps": deps}, args=args or [], chat_data={} if chat_data is None else chat_data
    )


def replied(update):
    call = update.effective_message.reply_text.await_args
    return call.args[0], call.kwargs.get("reply_markup")


def yes_data(markup):
    return markup.inline_keyboard[0][0].callback_data


# --- /enter --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args, expected",
    [
        ([], "Usage: /enter"),
        (["DOGE", "50"], "Unknown symbol"),
        (["SOL", "abc"], "positive number"),
        (["SOL", "3"], "Minimum order is 10.00 USDT"),
        (["SOL", "50", "-1"], "price must be a positive number"),
    ],
)
async def test_enter_validation(args, expected):
    update = cmd_update()
    await pos_handlers.enter_cmd(update, ctx(make_deps(), args))
    assert expected in replied(update)[0]


async def test_enter_default_price_confirm_opens_position_with_atr_stop():
    deps = make_deps()
    update = cmd_update()
    await pos_handlers.enter_cmd(update, ctx(deps, ["sol", "50"]))
    text, markup = replied(update)
    # stop = 121.5 - 2.2*2 = 117.1
    assert "Record SOL entry of <b>50.00 USDT</b> at 121.50, stop 117.10?" in text
    assert yes_data(markup) == "ent:SOLUSDT:50:121.5"

    chat_data = {}
    confirm = cb_update(yes_data(markup))
    await pos_handlers.on_enter_confirm(confirm, ctx(deps, chat_data=chat_data))
    (position,) = deps.repo.open_positions()
    assert position.amount_usdt == 50 and position.entry_price == 121.5
    assert position.stop_price == pytest.approx(117.1) and position.linked_signal_id is None
    assert position.id in deps.tracker.positions  # tracker reloaded
    assert "✅ Recorded SOL entry" in confirm.callback_query.message.edit_text.await_args.args[0]

    again = cb_update(yes_data(markup))  # same confirmation message tapped twice
    await pos_handlers.on_enter_confirm(again, ctx(deps, chat_data=chat_data))
    assert len(deps.repo.open_positions()) == 1
    assert "Already recorded" in again.callback_query.answer.await_args.args[0]


async def test_enter_with_explicit_price():
    deps = make_deps()
    update = cmd_update()
    await pos_handlers.enter_cmd(update, ctx(deps, ["SOL", "20", "100"]))
    assert yes_data(replied(update)[1]) == "ent:SOLUSDT:20:100"


# --- /close --------------------------------------------------------------------------------


async def test_close_validation():
    deps = make_deps()
    for args, expected in (
        ([], "Usage: /close"),
        (["x"], "Usage: /close"),
        (["99"], "No position #99"),
    ):
        update = cmd_update()
        await pos_handlers.close_cmd(update, ctx(deps, args))
        assert expected in replied(update)[0]


async def test_close_preview_confirm_and_double_close():
    deps = make_deps(price="110")
    p = add_position(deps.repo)
    update = cmd_update()
    await pos_handlers.close_cmd(update, ctx(deps, [str(p.id)]))
    text, markup = replied(update)
    # 0.999*110*0.999 - 100 = +9.78
    assert f"Close #{p.id} SOL at 110.00? P/L +9.78 USDT (+9.8%)" in text
    assert yes_data(markup) == f"cls:{p.id}:110"

    confirm = cb_update(yes_data(markup))
    await pos_handlers.on_close_confirm(confirm, ctx(deps))
    closed = deps.repo.get_position(p.id)
    assert closed.status == "closed" and closed.exit_price == 110
    assert closed.realised_pnl_usdt == pytest.approx(9.78011)
    assert closed.fees_usdt == pytest.approx(0.1 + 0.999 * 110 * 0.001)
    assert "🟢 Closed" in confirm.callback_query.message.edit_text.await_args.args[0]

    again = cb_update(yes_data(markup))
    await pos_handlers.on_close_confirm(again, ctx(deps))
    assert "Already closed" in again.callback_query.answer.await_args.args[0]

    update = cmd_update()
    await pos_handlers.close_cmd(update, ctx(deps, [str(p.id)]))
    assert "already closed" in replied(update)[0]


async def test_no_button_cancels():
    deps = make_deps()
    update = cb_update("no:x")
    await pos_handlers.on_no(update, ctx(deps))
    assert "Cancelled" in update.callback_query.message.edit_text.await_args.args[0]


# --- /positions, /history ------------------------------------------------------------------


async def test_positions_and_history_messages():
    deps = make_deps(price="110")
    empty = cmd_update()
    await pos_handlers.positions_cmd(empty, ctx(deps))
    assert "No open positions" in replied(empty)[0]

    p = add_position(deps.repo)
    update = cmd_update()
    await pos_handlers.positions_cmd(update, ctx(deps))
    text = replied(update)[0]
    assert f"#{p.id} SOL</b> 100.00 USDT @ 100.00" in text
    assert "P/L <b>+9.8%</b> (+9.78 USDT)" in text
    assert "Stop 96.00 · next target +10%" in text

    service.close_position(deps, p.id, 90.0)
    history = cmd_update()
    await pos_handlers.history_cmd(history, ctx(deps))
    text = replied(history)[0]
    assert "🔴" in text and "100.00 → 90.00" in text and "wins 0/1" in text


# --- position alert buttons ----------------------------------------------------------------


def test_alert_keyboards():
    def datas(markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    assert datas(pos_handlers.alert_keyboard(3, {"target"}, False)) == [
        "pos:3:tp",
        "pos:3:hold",
        "pos:3:trail",
    ]
    assert datas(pos_handlers.alert_keyboard(3, {"target"}, True)) == ["pos:3:tp", "pos:3:hold"]
    assert datas(pos_handlers.alert_keyboard(3, {"stop_hit"}, False)) == [
        "pos:3:close",
        "pos:3:keep",
    ]


def test_render_position_alert_text():
    deps = make_deps()
    p = add_position(deps.repo)
    text, _ = pos_handlers.render_position_alert(
        position=p,
        price=116.0,
        due=[
            DueAlert("target:10", "target", 10),
            DueAlert("target:15", "target", 15),
            DueAlert("trail_suggest", "trail_suggest"),
        ],
        pnl_usdt=15.77,
        pnl_pct=15.77,
        next_target=20,
        suggested_trailing_stop=111.6,
    )
    assert "targets +10%, +15% reached" in text
    assert "Consider a trailing stop at 111.60" in text
    assert "Advisory only" in text


async def test_take_profit_asks_to_close_and_hold_edits():
    deps = make_deps(price="116")
    p = add_position(deps.repo)
    tp = cb_update(f"pos:{p.id}:tp")
    await pos_handlers.on_position_action(tp, ctx(deps))
    text, markup = replied(tp)
    assert f"Close #{p.id} SOL at 116.00?" in text and yes_data(markup) == f"cls:{p.id}:116"

    hold = cb_update(f"pos:{p.id}:hold")
    await pos_handlers.on_position_action(hold, ctx(deps))
    assert "✋ Holding" in hold.callback_query.message.edit_text.await_args.args[0]


async def test_set_trailing_stop_button():
    deps = make_deps(price="116", atr=2.0)
    p = add_position(deps.repo, highest_price=116.0)
    deps.tracker.reload()
    update = cb_update(f"pos:{p.id}:trail")
    await pos_handlers.on_position_action(update, ctx(deps))
    stored = deps.repo.get_position(p.id)
    assert stored.trailing and stored.stop_price == 112  # 116 - 2*2
    assert deps.tracker.positions[p.id].trailing
    assert "Trailing stop on" in replied(update)[0]

    again = cb_update(f"pos:{p.id}:trail")
    await pos_handlers.on_position_action(again, ctx(deps))
    assert "already on" in again.callback_query.answer.await_args.args[0]


async def test_buttons_on_closed_position():
    deps = make_deps()
    p = add_position(deps.repo)
    service.close_position(deps, p.id, 100.0)
    update = cb_update(f"pos:{p.id}:tp")
    await pos_handlers.on_position_action(update, ctx(deps))
    assert "already closed" in update.callback_query.answer.await_args.args[0]
