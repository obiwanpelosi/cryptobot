"""/stats: your trades, signal outcomes, and rules vs AI vs models (requirements.md §5.10)."""

from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.evaluation.scoring import StatsReport, build_report
from bot.storage.db import Repo
from bot.telegram.deps import get_deps, reply
from bot.telegram.messages import stats_message


def report_from_repo(repo: Repo, rules, include_all: bool = False) -> StatsReport:
    return build_report(
        signals=repo.all_signals(),
        outcomes=repo.outcomes_by_signal(),
        ai_rows=repo.ai_answer_rows(),
        ai_costs=repo.ai_cost_by_model(),
        closed_positions=repo.closed_positions(limit=100_000),
        rules=rules,
        include_all=include_all,
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    include_all = bool(context.args) and context.args[0].lower() == "all"
    report = report_from_repo(deps.repo, deps.settings.strategy.dip_rules, include_all)
    await reply(update, context, stats_message(report))


def register(app: Application) -> None:
    app.add_handler(CommandHandler("stats", stats_cmd))
