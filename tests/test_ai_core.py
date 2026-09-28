import json
import time
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from bot.ai.advisor import AIAdvisor
from bot.ai.models import (
    active_model,
    active_shadows,
    parse_catalog,
    set_model_override,
    set_shadows,
)
from bot.ai.prompts import entry_context, exit_context, system_prompt, trade_history
from bot.ai.schema import EntryAdvice, ExitAdvice, strict_schema
from bot.settings import load_strategy
from bot.signals.rules import evaluate
from bot.signals.sizing import Suggestion, Target
from bot.storage.db import Repo, make_engine
from tests.ai_fakes import VALID_ENTRY, AIError, AIUnavailable, FakeProvider, result
from tests.test_rules import RULES
from tests.test_rules import snap as make_snap
from tests.test_tracker import add_position


def strategy(**ai):
    s = load_strategy()
    return s.model_copy(update={"ai": s.ai.model_copy(update={"enabled": True, **ai})})


# --- schema ----------------------------------------------------------------------------------


def test_strict_schema_has_no_extras_and_requires_everything():
    for model in (EntryAdvice, ExitAdvice):
        schema = strict_schema(model)
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert "title" not in json.dumps(schema) and "minimum" not in json.dumps(schema)
    assert strict_schema(EntryAdvice)["properties"]["action"]["enum"] == ["enter", "wait", "skip"]
    assert "hold" in strict_schema(ExitAdvice)["properties"]["action"]["enum"]


def test_advice_validation():
    assert EntryAdvice.model_validate(VALID_ENTRY).confidence == 0.62
    for bad in ({"confidence": 1.5}, {"action": "hold"}, {"time_horizon": "weeks"}, {"extra": 1}):
        with pytest.raises(ValidationError):
            EntryAdvice.model_validate({**VALID_ENTRY, **bad})


# --- prompts ----------------------------------------------------------------------------


def test_system_prompt_has_strategy_and_guardrails():
    text = system_prompt(load_strategy())
    for expected in (
        "1.5% of the balance",
        "30% of the balance",
        "10%, 15%, 20%",
        "Never promise or predict returns",
        "not instructions",
        "JSON only",
    ):
        assert expected in text


def test_entry_context_contents():
    snapshot = make_snap()
    suggestion = Suggestion(
        entry=D("100"),
        stop=D("96"),
        stop_distance_pct=D("4"),
        size_usdt=D("60"),
        qty=D("0.6"),
        targets=[Target(pct=D("10"), price=D("110.23"), gain_usdt=D("6"))],
    )
    ctx = json.loads(
        entry_context(
            snapshot=snapshot,
            rule_result=evaluate(snapshot, RULES),
            suggestion=suggestion,
            portfolio={"total_value_usdt": 199.0},
            history=[{"pnl_pct": 5.0}],
        )
    )
    assert ctx["market_snapshot"]["symbol"] == "SOLUSDT"
    assert [c["name"] for c in ctx["rule_checks"]] == ["drop_from_high", "rsi_1h"]
    assert ctx["suggestion"]["size_usdt"] == 60 and ctx["suggestion"]["stop"] == 96
    assert ctx["portfolio"]["total_value_usdt"] == 199.0
    assert ctx["recent_trades_this_symbol"] == [{"pnl_pct": 5.0}]
    assert len(json.dumps(ctx)) < 6000


def test_exit_context_and_history():
    repo = Repo(make_engine(None))
    p = add_position(repo, entry_time=int(time.time()) - 7200, stop_price=96.0)
    ctx = json.loads(
        exit_context(
            position=p,
            price=110.0,
            pnl_usdt=9.78,
            pnl_pct=9.78,
            alert_kinds=["target"],
            snapshot=None,
            history=[],
        )
    )
    assert ctx["alerts"] == ["target"] and ctx["position"]["pnl_pct_after_fees"] == 9.78
    assert ctx["position"]["held_hours"] == pytest.approx(2.0, abs=0.1)
    repo.close_position(
        p.id,
        exit_time=p.entry_time + 3600,
        exit_price=95.9,
        realised_pnl_pct=-4.3,
        realised_pnl_usdt=-4.3,
    )
    (h,) = trade_history(repo.recent_closed_positions("SOLUSDT"))
    assert h == {"pnl_pct": -4.3, "held_hours": 1.0, "exit": "near_or_at_stop", "paper": True}


# --- model selection ----------------------------------------------------------------------


def test_catalog_parsing():
    data = {
        "data": [
            {
                "id": "a/strict",
                "supported_parameters": ["structured_outputs"],
                "pricing": {"prompt": "0.000002", "completion": "0.00001"},
            },
            {"id": "b/loose", "supported_parameters": ["response_format"], "pricing": {}},
        ]
    }
    models = parse_catalog(data)
    assert models["a/strict"].strict_json and models["a/strict"].prompt_usd_per_m == pytest.approx(
        2
    )
    assert not models["b/loose"].strict_json and models["b/loose"].prompt_usd_per_m is None


def test_runtime_override_beats_config_and_survives_restart(tmp_path):
    cfg = load_strategy().ai
    repo = Repo(make_engine(tmp_path / "bot.db"))
    assert active_model(repo, cfg, "strong") == cfg.strong_model
    set_model_override(repo, "strong", "openai/gpt-5.4-mini")
    restarted = Repo(make_engine(tmp_path / "bot.db"))
    assert active_model(restarted, cfg, "strong") == "openai/gpt-5.4-mini"
    assert active_model(restarted, cfg, "light") == cfg.light_model
    set_model_override(restarted, "strong", None)
    assert active_model(restarted, cfg, "strong") == cfg.strong_model
    assert active_shadows(restarted, cfg) == []
    set_shadows(restarted, ["x/b", "x/a", "x/a"])
    assert active_shadows(restarted, cfg) == ["x/a", "x/b"]


# --- advisor -------------------------------------------------------------------------------


def make_advisor(provider, repo=None, **ai):
    repo = repo or Repo(make_engine(None))
    return AIAdvisor(provider=provider, repo=repo, strategy=strategy(**ai)), repo


def calls(repo):
    from sqlalchemy import select

    from bot.storage.models import AICall

    with repo._session() as s:
        return list(s.scalars(select(AICall).order_by(AICall.id)))


async def test_success_returns_advice_and_logs_call():
    provider = FakeProvider()
    advisor, repo = make_advisor(provider)
    advice, model = await advisor.analyze_entry("{}", signal_id=None)
    assert advice.action == "enter" and model == advisor.cfg.strong_model
    (row,) = calls(repo)
    assert row.valid_json and row.cost_usd == 0.0012 and row.served_model == "served/model"
    assert row.purpose == "entry" and row.role == "primary" and row.tokens_in == 2500
    assert provider.calls[0]["schema"]["additionalProperties"] is False


async def test_invalid_output_retried_once_then_gives_up():
    strong = load_strategy().ai.strong_model
    bad = result(None, text='{"action": "maybe"}')
    provider = FakeProvider({strong: [bad, bad]})
    advisor, repo = make_advisor(provider)
    assert await advisor.analyze_entry("{}") is None
    rows = calls(repo)
    assert len(rows) == 2 and not any(r.valid_json for r in rows)
    assert "Your reply was not valid" in provider.calls[1]["messages"][-1]["content"]


async def test_invalid_then_valid_succeeds():
    strong = load_strategy().ai.strong_model
    provider = FakeProvider({strong: [result(None, text="not json"), result(VALID_ENTRY)]})
    advisor, _ = make_advisor(provider)
    advice, _ = await advisor.analyze_entry("{}")
    assert advice.action == "enter"


async def test_errors_return_none_and_unavailable_notifies_once():
    notices = []
    strong = load_strategy().ai.strong_model
    provider = FakeProvider(
        {strong: [AIError("timeout"), AIUnavailable("HTTP 402"), result(VALID_ENTRY)]}
    )
    advisor, repo = make_advisor(provider)

    async def notify(reason):
        notices.append(reason)

    advisor.on_unavailable = notify
    assert await advisor.analyze_entry("{}") is None  # timeout
    assert await advisor.analyze_entry("{}") is None  # 402
    assert await advisor.analyze_entry("{}") is None  # now short-circuits
    assert notices == ["HTTP 402"]
    assert len(provider.calls) == 2
    assert "unavailable" in advisor.last_skip_reason


async def test_hourly_limit_blocks_calls():
    provider = FakeProvider()
    advisor, repo = make_advisor(provider, max_calls_per_hour=2)
    assert await advisor.analyze_entry("{}") is not None
    assert await advisor.analyze_entry("{}") is not None
    assert await advisor.analyze_entry("{}") is None
    assert advisor.last_skip_reason == "hourly AI limit reached"
    assert len(provider.calls) == 2


async def test_experiments_do_not_count_toward_limit():
    provider = FakeProvider()
    advisor, repo = make_advisor(provider, max_calls_per_hour=1)
    await advisor.run_model("x/model", EntryAdvice, "{}", purpose="experiment")
    assert await advisor.analyze_entry("{}") is not None


async def test_shadow_models_logged_not_returned():
    shadow = "openai/gpt-5.4-mini"
    provider = FakeProvider({shadow: [result({**VALID_ENTRY, "action": "skip"})]})
    advisor, repo = make_advisor(provider, shadow_models=[shadow])
    advice, model = await advisor.analyze_entry("{}", signal_id=None)
    await advisor.drain()
    assert advice.action == "enter" and model != shadow
    roles = {(r.model, r.role) for r in calls(repo)}
    assert (shadow, "shadow") in roles and (model, "primary") in roles


async def test_light_role_uses_light_model():
    provider = FakeProvider()
    advisor, _ = make_advisor(provider)
    _, model = await advisor.analyze_entry("{}", purpose="analysis", role="light")
    assert model == advisor.cfg.light_model
