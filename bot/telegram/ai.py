"""/ai: status, runtime model switching and shadow models (Phase 6)."""

from __future__ import annotations

import time

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.ai.models import (
    ROLES,
    active_model,
    active_shadows,
    set_model_override,
    set_shadows,
)
from bot.telegram.deps import Deps, get_deps, reply
from bot.telegram.messages import ai_status_message

USAGE = (
    "Usage: /ai · /ai model strong|light &lt;model-id&gt; · /ai model strong|light reset"
    " · /ai shadow add|remove &lt;model-id&gt;"
)


def _month_start(now: float) -> int:
    t = time.gmtime(now)
    return int(time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1)))


def status_text(deps: Deps, now: float | None = None) -> str:
    now = time.time() if now is None else now
    cfg = deps.settings.strategy.ai
    repo = deps.repo
    day_start = int(now - (now % 86400))
    return ai_status_message(
        enabled=cfg.enabled,
        has_key=bool(deps.settings.secrets.openrouter_api_key),
        strong=active_model(repo, cfg, "strong"),
        light=active_model(repo, cfg, "light"),
        strong_overridden=bool(repo.get_state("ai_model_strong")),
        light_overridden=bool(repo.get_state("ai_model_light")),
        shadows=active_shadows(repo, cfg),
        candidates=cfg.candidates,
        calls_hour=repo.ai_calls_since(int(now) - 3600),
        limit=cfg.max_calls_per_hour,
        spend_today=repo.ai_spend_since(day_start),
        spend_month=repo.ai_spend_since(_month_start(now)),
        unavailable=deps.advisor.unavailable_reason if deps.advisor else None,
    )


async def _validate(deps: Deps, model_id: str) -> str | None:
    if deps.catalog is None:
        return None
    return await deps.catalog.validate(model_id)


async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    args = context.args or []
    if not args:
        await reply(update, context, status_text(deps))
        return
    cfg = deps.settings.strategy.ai

    if args[0] == "model" and len(args) == 3 and args[1] in ROLES:
        role, model_id = args[1], args[2]
        if model_id == "reset":
            set_model_override(deps.repo, role, None)
            default = active_model(deps.repo, cfg, role)
            await reply(update, context, f"✅ {role} model reset to config: <code>{default}</code>")
            return
        problem = await _validate(deps, model_id)
        if problem:
            await reply(update, context, f"❌ {problem}")
            return
        set_model_override(deps.repo, role, model_id)
        await reply(update, context, f"✅ {role} model is now <code>{model_id}</code>")
        return

    if args[0] == "shadow" and len(args) == 3 and args[1] in ("add", "remove"):
        model_id = args[2]
        shadows = active_shadows(deps.repo, cfg)
        if args[1] == "add":
            problem = await _validate(deps, model_id)
            if problem:
                await reply(update, context, f"❌ {problem}")
                return
            set_shadows(deps.repo, shadows + [model_id])
        else:
            set_shadows(deps.repo, [m for m in shadows if m != model_id])
        current = active_shadows(deps.repo, cfg)
        listing = ", ".join(f"<code>{m}</code>" for m in current) or "none"
        await reply(update, context, f"✅ Shadow models: {listing}")
        return

    await reply(update, context, USAGE)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("ai", ai_cmd))
