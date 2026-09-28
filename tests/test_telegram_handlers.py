import logging
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ApplicationHandlerStop

from bot.exchange.models import Balance, Tick
from bot.settings import Secrets, Settings, load_strategy
from bot.telegram import handlers
from bot.telegram.deps import Deps

ALLOWED = 111


def make_deps(balances=None, balance_error=None):
    secrets = Secrets(_env_file=None, telegram_allowed_chat_ids=[ALLOWED])
    settings = Settings(secrets=secrets, strategy=load_strategy())
    client = MagicMock()
    if balance_error:
        client.get_balances = AsyncMock(side_effect=balance_error)
    else:
        client.get_balances = AsyncMock(return_value=balances or {})
    stream = SimpleNamespace(
        symbols=["SOLUSDT", "LINKUSDT", "BTCUSDT"],
        ticks={"SOLUSDT": Tick("SOLUSDT", D("150"), D("2"), 10**13)},
    )
    return Deps(client=client, stream=stream, settings=settings)


def make_update(chat_id=ALLOWED, username="me"):
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=chat_id, username=username),
        effective_message=message,
    )


def make_context(deps):
    return SimpleNamespace(bot_data={"deps": deps}, error=None)


def replied_text(update):
    update.effective_message.reply_text.assert_awaited_once()
    return update.effective_message.reply_text.await_args.args[0]


async def test_guard_allows_listed_chat():
    await handlers.guard(make_update(ALLOWED), make_context(make_deps()))


async def test_guard_blocks_and_logs_others(caplog):
    update = make_update(chat_id=999, username="stranger")
    with caplog.at_level(logging.WARNING), pytest.raises(ApplicationHandlerStop):
        await handlers.guard(update, make_context(make_deps()))
    assert "chat_id=999" in caplog.text and "@stranger" in caplog.text
    update.effective_message.reply_text.assert_not_awaited()


async def test_guard_blocks_update_without_chat():
    update = SimpleNamespace(effective_chat=None, effective_user=None)
    with pytest.raises(ApplicationHandlerStop):
        await handlers.guard(update, make_context(make_deps()))


async def test_start_reply_has_paper_prefix():
    update = make_update()
    await handlers.start_cmd(update, make_context(make_deps()))
    text = replied_text(update)
    assert text.startswith("[PAPER] ")
    assert "/balance" in text


async def test_price_uses_stream_ticks():
    update = make_update()
    await handlers.price_cmd(update, make_context(make_deps()))
    text = replied_text(update)
    assert "SOL: <b>150.00</b>" in text
    assert "LINK: n/a" in text


async def test_balance_reply():
    deps = make_deps(
        balances={
            "USDT": Balance("USDT", D("50"), D("0")),
            "SOL": Balance("SOL", D("1"), D("0")),
        }
    )
    update = make_update()
    await handlers.balance_cmd(update, make_context(deps))
    deps.client.get_balances.assert_awaited_once_with(["USDT", "SOL", "LINK"])
    assert "Total ≈ 200.00 USDT" in replied_text(update)


async def test_balance_error_friendly_reply():
    deps = make_deps(balance_error=RuntimeError("binance down"))
    update = make_update()
    await handlers.balance_cmd(update, make_context(deps))
    assert "Couldn't fetch balances" in replied_text(update)


async def test_error_handler_notifies_throttled(monkeypatch):
    deps = make_deps()
    deps.notifier = SimpleNamespace(send=AsyncMock())
    ctx = SimpleNamespace(bot_data={"deps": deps}, error=ValueError("x"))
    now = [1000.0]
    monkeypatch.setattr(handlers.time, "monotonic", lambda: now[0])
    await handlers.error_handler(None, ctx)
    now[0] += 10
    await handlers.error_handler(None, ctx)
    assert deps.notifier.send.await_count == 1
    now[0] += handlers.ERROR_NOTICE_INTERVAL
    await handlers.error_handler(None, ctx)
    assert deps.notifier.send.await_count == 2


def make_analysis_context(deps, args):
    return SimpleNamespace(bot_data={"deps": deps}, error=None, args=args)


def test_resolve_symbol():
    symbols = ["SOLUSDT", "LINKUSDT"]
    assert handlers.resolve_symbol("sol", symbols) == "SOLUSDT"
    assert handlers.resolve_symbol("LINKUSDT", symbols) == "LINKUSDT"
    assert handlers.resolve_symbol("doge", symbols) is None


async def test_analysis_usage_on_missing_or_bad_symbol():
    deps = make_deps()
    for args in ([], ["DOGE"]):
        update = make_update()
        await handlers.analysis_cmd(update, make_analysis_context(deps, args))
        text = replied_text(update)
        assert "Usage: /analysis" in text and "SOL, LINK" in text


async def test_analysis_warming_up():
    deps = make_deps()
    deps.snapshots = SimpleNamespace(build=lambda symbol: None)
    update = make_update()
    await handlers.analysis_cmd(update, make_analysis_context(deps, ["sol"]))
    assert "warming up" in replied_text(update)


async def test_analysis_replies_with_snapshot():
    from tests.test_snapshot import NOW_MS, make_builder

    builder, _ = make_builder()
    deps = make_deps()
    deps.snapshots = SimpleNamespace(build=lambda symbol: builder.build(symbol, now_ms=NOW_MS))
    update = make_update()
    await handlers.analysis_cmd(update, make_analysis_context(deps, ["sol"]))
    text = replied_text(update)
    assert text.startswith("[PAPER] <b>SOL</b> 121.50")
    assert "<pre>" in text and "15m" in text and "1d" in text
    assert "Fear &amp; Greed: 40 (Fear)" in text
    assert "BTC 81,000.00" in text


def test_polling_conflict_logged_once_per_minute(caplog, monkeypatch):
    from telegram.error import Conflict

    handlers._last_polling_warning.clear()
    now = [0.0]
    monkeypatch.setattr(handlers.time, "monotonic", lambda: now[0])
    with caplog.at_level(logging.WARNING):
        handlers.polling_error(Conflict("terminated by other getUpdates request"))
        handlers.polling_error(Conflict("again"))
        now[0] = 61
        handlers.polling_error(Conflict("later"))
    assert caplog.text.count("another instance of this bot") == 2
    assert "Traceback" not in caplog.text


def test_application_builds_with_all_handlers():
    from telegram.ext import CallbackQueryHandler, CommandHandler, ConversationHandler

    app = handlers.build_application("123456:TEST-TOKEN", make_deps())
    registered = [h for group in app.handlers.values() for h in group]
    commands = {c for h in registered if isinstance(h, CommandHandler) for c in h.commands}
    assert {
        "start", "price", "balance", "analysis", "pause", "resume",
        "positions", "enter", "close", "history",
    } <= commands
    assert any(isinstance(h, ConversationHandler) for h in registered)
    # cfm, cxl (signal confirm) + ent, cls, no, pos (positions)
    assert sum(isinstance(h, CallbackQueryHandler) for h in registered) == 6
