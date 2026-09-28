import asyncio
import json
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from bot.ai.advisor import AIAdvisor
from bot.ai.compare import Case, markdown_report, run_cases, signal_case
from bot.ai.schema import EntryAdvice
from bot.exchange.models import Tick
from bot.positions.tracker import PositionTracker
from bot.settings import Secrets, Settings, load_strategy
from bot.signals.sizing import Suggestion
from bot.storage.db import Repo, make_engine
from bot.telegram import ai as ai_commands
from bot.telegram.deps import Deps
from bot.telegram.messages import ai_entry_section, ai_exit_section
from bot.telegram.positions import render_position_alert
from tests.ai_fakes import VALID_ENTRY, VALID_EXIT, AIError, FakeProvider, result
from tests.test_ai_core import strategy
from tests.test_engine import make_engine_under_test
from tests.test_tracker import add_position

SUGGESTION = Suggestion(
    entry=D("121"), stop=D("117.31"), stop_distance_pct=D("3"), size_usdt=D("60"), qty=D("0.5")
)


def recording_notifier():
    message = SimpleNamespace(edit_text=AsyncMock())
    notifier = SimpleNamespace(send=AsyncMock(return_value=[message]), edit=AsyncMock())
    return notifier, message


async def settle(*holders):
    for _ in range(5):
        tasks = [t for h in holders for t in list(getattr(h, "_tasks", []))]
        if not tasks:
            break
        await asyncio.gather(*tasks)


# --- message guardrails ----------------------------------------------------------------------


def test_entry_section_guardrails_and_footer():
    advice = EntryAdvice.model_validate({**VALID_ENTRY, "suggested_size_adjustment": "increase"})
    text = ai_entry_section(advice, "anthropic/claude-sonnet-5", SUGGESTION)
    assert "AI (claude-sonnet-5): ENTER" in text and "confidence 62%" in text
    assert "AI would use stop 115.20 (rule stop 117.31 kept)" in text
    assert "capped at your rule size" in text
    assert text.endswith("<i>Not financial advice. The decision is yours.</i>")


def test_same_stop_not_repeated():
    advice = EntryAdvice.model_validate({**VALID_ENTRY, "suggested_stop": 117.3})
    assert "AI would use stop" not in ai_entry_section(advice, "m", SUGGESTION)
    advice0 = EntryAdvice.model_validate({**VALID_ENTRY, "suggested_stop": 0})
    assert "AI would use stop" not in ai_entry_section(advice0, "m", SUGGESTION)


# --- entry alerts ----------------------------------------------------------------------------


async def test_entry_alert_sent_first_then_edited_with_ai():
    engine, repo, _ = make_engine_under_test()
    notifier, _ = recording_notifier()
    engine.notifier = notifier
    engine.advisor = AIAdvisor(provider=FakeProvider(), repo=repo, strategy=strategy())
    signal = await engine.evaluate_symbol("SOLUSDT")
    notifier.send.assert_awaited_once()  # rule alert went out immediately
    await settle(engine)
    edited_text = notifier.edit.await_args.args[1]
    assert edited_text.startswith(notifier.send.await_args.args[0])  # rule text kept
    assert "🤖 <b>AI (" in edited_text
    assert notifier.edit.await_args.kwargs["reply_markup"] == (
        "kb",
        signal.id,
        True,
    )  # buttons kept
    stored = json.loads(repo.get_signal(signal.id).ai_advice_json)
    assert stored["action"] == "enter" and stored["model"] == strategy().ai.strong_model


async def test_entry_alert_ai_failure_leaves_rule_alert():
    engine, repo, _ = make_engine_under_test()
    notifier, _ = recording_notifier()
    engine.notifier = notifier
    failing = FakeProvider(default=AIError("timeout"))
    engine.advisor = AIAdvisor(provider=failing, repo=repo, strategy=strategy())
    signal = await engine.evaluate_symbol("SOLUSDT")
    await settle(engine)
    notifier.send.assert_awaited_once()
    notifier.edit.assert_not_awaited()
    assert repo.get_signal(signal.id).ai_advice_json is None


async def test_no_advisor_means_no_edit():
    engine, _, _ = make_engine_under_test()
    notifier, _ = recording_notifier()
    engine.notifier = notifier
    await engine.evaluate_symbol("SOLUSDT")
    await settle(engine)
    notifier.edit.assert_not_awaited()


# --- exit alerts -----------------------------------------------------------------------------


async def test_position_alert_edited_with_exit_advice():
    repo = Repo(make_engine(None))
    position = add_position(repo)
    notifier, _ = recording_notifier()
    tracker = PositionTracker(
        repo=repo,
        strategy=load_strategy(),
        notifier=notifier,
        render=render_position_alert,
        tick_size=lambda s: 0.01,
        clock=lambda: 0.0,
    )
    provider = FakeProvider(default=result(VALID_EXIT))
    tracker.advisor = AIAdvisor(provider=provider, repo=repo, strategy=strategy())
    tracker.ai_section = ai_exit_section
    tracker.load()
    tracker.on_tick(Tick("SOLUSDT", D("110.5"), D(0), 0))
    await settle(tracker)
    text = notifier.edit.await_args.args[1]
    assert "target +10% reached" in text and "AI (" in text and "HOLD" in text
    assert provider.calls[0]["model"] == strategy().ai.strong_model  # target -> strong

    notifier.edit.reset_mock()
    tracker.on_tick(Tick("SOLUSDT", D("97.5"), D(0), 0))  # near stop -> light model
    await settle(tracker)
    assert provider.calls[-1]["model"] == strategy().ai.light_model
    assert repo.sent_alert_keys([position.id])[position.id] >= {"target:10", "near_stop:96"}


# --- compare -----------------------------------------------------------------------------------


async def test_compare_rows_and_report():
    repo = Repo(make_engine(None))
    provider = FakeProvider({"bad/model": [AIError("HTTP 404: no such model")]})
    advisor = AIAdvisor(provider=provider, repo=repo, strategy=strategy())
    cases = [Case("live SOL", "entry", "{}")]
    rows = await run_cases(advisor, ["good/model", "bad/model"], cases)
    by_model = {r["model"]: r for r in rows}
    assert by_model["good/model"]["ok"] and by_model["good/model"]["action"] == "enter"
    assert not by_model["bad/model"]["ok"] and "404" in by_model["bad/model"]["error"]
    report = markdown_report(rows, cases)
    assert "| good/model | enter | 62%" in report and "❌ HTTP 404" in report
    assert "1/2 valid answers" in report


def test_signal_replay_rebuilds_context():
    engine, repo, _ = make_engine_under_test()
    signal = asyncio.run(engine.evaluate_symbol("SOLUSDT"))
    case = signal_case(repo.get_signal(signal.id), repo)
    ctx = json.loads(case.context)
    assert case.kind == "entry" and case.signal_id == signal.id
    assert ctx["market_snapshot"]["symbol"] == "SOLUSDT"
    assert [c["name"] for c in ctx["rule_checks"]] == ["drop_from_high", "rsi_1h"]
    assert ctx["suggestion"]["size_usdt"] == 60


# --- /ai command -------------------------------------------------------------------------------


def ai_deps(catalog_problem=None):
    settings = Settings(secrets=Secrets(_env_file=None), strategy=strategy())
    catalog = SimpleNamespace(validate=AsyncMock(return_value=catalog_problem))
    return Deps(
        client=MagicMock(),
        stream=SimpleNamespace(ticks={}),
        settings=settings,
        repo=Repo(make_engine(None)),
        catalog=catalog,
    )


def update():
    return SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()))


def ctx(deps, args):
    return SimpleNamespace(bot_data={"deps": deps}, args=args)


def replied(u):
    return u.effective_message.reply_text.await_args.args[0]


async def test_ai_status_and_model_switch():
    deps = ai_deps()
    u = update()
    await ai_commands.ai_cmd(u, ctx(deps, []))
    text = replied(u)
    assert "OPENROUTER_API_KEY missing" in text and "anthropic/claude-sonnet-5" in text
    assert "Calls this hour: 0/20" in text

    u = update()
    await ai_commands.ai_cmd(u, ctx(deps, ["model", "strong", "deepseek/deepseek-v4-pro"]))
    assert "strong model is now" in replied(u)
    u = update()
    await ai_commands.ai_cmd(u, ctx(deps, []))
    assert "deepseek/deepseek-v4-pro</code> (override)" in replied(u)

    u = update()
    await ai_commands.ai_cmd(u, ctx(deps, ["model", "strong", "reset"]))
    assert "reset to config" in replied(u)


async def test_ai_rejects_unknown_model_and_manages_shadows():
    deps = ai_deps(catalog_problem="nonsense/model is not an OpenRouter model id")
    u = update()
    await ai_commands.ai_cmd(u, ctx(deps, ["model", "strong", "nonsense/model"]))
    assert "❌ nonsense/model is not an OpenRouter model id" in replied(u)
    assert deps.repo.get_state("ai_model_strong") is None

    deps = ai_deps()
    u = update()
    await ai_commands.ai_cmd(u, ctx(deps, ["shadow", "add", "openai/gpt-5.4-mini"]))
    assert "openai/gpt-5.4-mini" in replied(u)
    u = update()
    await ai_commands.ai_cmd(u, ctx(deps, ["shadow", "remove", "openai/gpt-5.4-mini"]))
    assert "Shadow models: none" in replied(u)
    u = update()
    await ai_commands.ai_cmd(u, ctx(deps, ["bogus"]))
    assert "Usage: /ai" in replied(u)
