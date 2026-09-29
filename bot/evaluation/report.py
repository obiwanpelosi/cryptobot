"""Evaluation report: the /stats numbers plus a per-signal breakdown.

uv run python -m bot.evaluation.report          # signals under the current rules
uv run python -m bot.evaluation.report --all    # include signals from other rule settings
uv run python -m bot.evaluation.report --update # evaluate any due outcomes first (Binance)
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time

from bot.evaluation.scoring import HORIZON_ORDER, latest_answers, split_by_rules, trade_result
from bot.settings import PROJECT_ROOT, load_settings
from bot.storage.db import Repo, make_engine
from bot.telegram.messages import stats_message
from bot.telegram.stats import report_from_repo

REPORT_DIR = PROJECT_ROOT / "data" / "experiments"


def _plain(html: str) -> str:
    text = re.sub(r"<[^>]+>", "", html)
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def per_signal_table(repo: Repo, rules, include_all: bool) -> str:
    signals, _ = split_by_rules(repo.all_signals(), rules, include_all)
    outcomes = repo.outcomes_by_signal()
    answers = latest_answers(repo.ai_answer_rows())
    models = sorted(answers)
    header = ["signal", "symbol", "when", "price", "you"]
    header += [f"+{h}" for h in HORIZON_ORDER] + [m.split("/", 1)[-1] for m in models]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for s in signals:
        cells = [
            f"#{s.id}",
            s.symbol,
            time.strftime("%m-%d %H:%M", time.localtime(s.ts)),
            f"{s.price:g}",
            s.user_action,
        ]
        for h in HORIZON_ORDER:
            o = outcomes.get(s.id, {}).get(h)
            cells.append(f"{o.first_hit} {trade_result(o):+.1f}%" if o else "pending")
        for m in models:
            a = answers[m].get(s.id)
            cells.append(f"{a[0]} {a[1] * 100:.0f}%" if a else "-")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


async def update_outcomes(settings, repo: Repo) -> int:
    from bot.evaluation.outcomes import OutcomeEvaluator
    from bot.exchange.client import BinanceClient

    client = await BinanceClient.create(settings.secrets)
    try:
        return await OutcomeEvaluator(
            repo=repo, klines=client, strategy=settings.strategy
        ).run_once()
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--all", action="store_true", help="include signals from other rule settings"
    )
    parser.add_argument("--update", action="store_true", help="evaluate due outcomes first")
    args = parser.parse_args(argv)

    settings = load_settings()
    rules = settings.strategy.dip_rules
    repo = Repo(make_engine(PROJECT_ROOT / "data" / "bot.db"))
    if args.update:
        print(f"Evaluated {asyncio.run(update_outcomes(settings, repo))} new outcome(s).")

    summary = _plain(stats_message(report_from_repo(repo, rules, args.all)))
    breakdown = per_signal_table(repo, rules, args.all)
    print(summary)
    print("\nPer signal (outcome = first hit and net trade result):")
    print(breakdown)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"stats-{time.strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(
        f"# Stats {time.strftime('%Y-%m-%d %H:%M')}\n\n```\n{summary}\n```\n\n{breakdown}\n"
    )
    print(f"\nSaved: {path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
