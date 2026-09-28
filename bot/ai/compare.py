"""Side-by-side model experiments: the same prompt through several OpenRouter models.

    uv run python -m bot.ai.compare --live SOL
    uv run python -m bot.ai.compare --last-signals 5 \\
        --models anthropic/claude-sonnet-5,openai/gpt-5.4-mini
    uv run python -m bot.ai.compare --signal 12
    uv run python -m bot.ai.compare --exit 3

Contexts are built by the same functions the live bot uses, so an experiment tests exactly
what production would send. Every call is logged to ai_calls with purpose=experiment
(not counted against the hourly limit). Results are saved under data/experiments/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.ai.advisor import AIAdvisor
from bot.ai.models import ModelCatalog
from bot.ai.openrouter import OpenRouterProvider
from bot.ai.prompts import entry_context, exit_context, trade_history
from bot.ai.schema import EntryAdvice, ExitAdvice
from bot.market.snapshot import MarketSnapshot
from bot.positions.logic import position_pnl
from bot.settings import PROJECT_ROOT, Settings, load_settings
from bot.signals.rules import Check, RuleResult
from bot.signals.sizing import Suggestion, fee_fraction
from bot.storage.db import Repo, make_engine
from bot.storage.models import Signal

EXPERIMENTS_DIR = PROJECT_ROOT / "data" / "experiments"
CONFIRM_ABOVE_USD = 0.50
EST_OUTPUT_TOKENS = 450
MAX_CONCURRENCY = 4


@dataclass
class Case:
    label: str
    kind: str  # entry | exit
    context: str
    signal_id: int | None = None
    position_id: int | None = None


# --- building cases ----------------------------------------------------------------------


def _resolve(symbol: str, symbols: list[str]) -> str:
    wanted = symbol.upper()
    for s in symbols:
        if wanted in (s, s.removesuffix("USDT")):
            return s
    raise SystemExit(f"Unknown symbol {symbol}. One of: {', '.join(symbols)}")


def signal_case(signal: Signal, repo: Repo) -> Case:
    """Rebuild the entry context for a stored signal, as the rules saw it at the time."""
    snapshot = MarketSnapshot.model_validate_json(signal.snapshot_json)
    rows = json.loads(signal.rule_values_json or "[]")
    checks = [Check(**r) for r in rows if "label" in r]
    btc = next((r.get("value") for r in rows if r.get("name") == "btc_change_1h_pct"), None)
    rule_result = RuleResult(
        symbol=signal.symbol,
        fired=True,
        high_risk=signal.high_risk,
        btc_change_1h_pct=btc,
        checks=checks,
    )
    suggestion = (
        Suggestion.model_validate_json(signal.suggestion_json) if signal.suggestion_json else None
    )
    earlier = [
        p for p in repo.recent_closed_positions(signal.symbol, 50) if (p.exit_time or 0) < signal.ts
    ]
    history = trade_history(earlier[:20])
    context = entry_context(
        snapshot=snapshot,
        rule_result=rule_result,
        suggestion=suggestion,
        portfolio={"note": "portfolio at signal time not stored"},
        history=history,
    )
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(signal.ts))
    return Case(f"signal #{signal.id} {signal.symbol} {when}", "entry", context, signal.id)


async def live_entry_case(settings: Settings, symbol: str, repo: Repo) -> Case:
    from bot.exchange.client import BinanceClient
    from bot.market.candles import CandleStore
    from bot.market.context import MarketContext
    from bot.market.snapshot import SnapshotBuilder
    from bot.signals.engine import SignalEngine
    from bot.signals.rules import evaluate
    from bot.telegram.notifier import NullNotifier

    cfg = settings.strategy
    symbols = list(dict.fromkeys(cfg.symbols + cfg.context_symbols))
    client = await BinanceClient.create(settings.secrets)
    try:
        info = await client.check_symbols(symbols)
        from bot.signals.sizing import SymbolFilters

        filters = {s: SymbolFilters.from_exchange_info(info[s]) for s in cfg.symbols}
        store = CandleStore(symbols, cfg.timeframes, cfg.history_candles)
        await store.load(client)
        market = MarketContext(client, cfg.symbols, cfg.macro_events)
        await market.refresh()
        ticks = await client.get_24h_tickers(symbols)
        builder = SnapshotBuilder(
            store,
            market,
            ticks,
            high_lookback_hours=cfg.dip_rules.high_lookback_hours,
            btc_symbol=cfg.context_symbols[0] if cfg.context_symbols else None,
        )
        snapshot = builder.build(symbol)
        rule_result = evaluate(snapshot, cfg.dip_rules)
        engine = SignalEngine(
            strategy=cfg,
            snapshots=builder,
            repo=repo,
            balances=client,
            filters=filters,
            notifier=NullNotifier(),
        )
        suggestion = await engine._suggest(symbol, snapshot.price, snapshot)
        history = trade_history(repo.recent_closed_positions(symbol, 20))
        context = entry_context(
            snapshot=snapshot,
            rule_result=rule_result,
            suggestion=suggestion,
            portfolio=engine._portfolio,
            history=history,
        )
        fired = "rules fired" if rule_result.fired else "rules did NOT fire (as-if signal)"
        return Case(f"live {symbol} @ {snapshot.price:g} ({fired})", "entry", context)
    finally:
        await client.close()


async def live_exit_case(settings: Settings, position_id: int, repo: Repo) -> Case:
    from bot.exchange.client import BinanceClient

    position = repo.get_position(position_id)
    if position is None:
        raise SystemExit(f"No position #{position_id}")
    client = await BinanceClient.create(settings.secrets)
    try:
        tick = (await client.get_24h_tickers([position.symbol]))[position.symbol]
    finally:
        await client.close()
    price = float(tick.price)
    fee = float(fee_fraction(settings.strategy.fees))
    pnl_usdt, pnl_pct = position_pnl(position.amount_usdt, position.quantity, price, fee)
    context = exit_context(
        position=position,
        price=price,
        pnl_usdt=pnl_usdt,
        pnl_pct=pnl_pct,
        alert_kinds=["manual_review"],
        snapshot=None,
        history=trade_history(repo.recent_closed_positions(position.symbol, 20)),
    )
    label = f"exit position #{position.id} {position.symbol} ({pnl_pct:+.1f}%)"
    return Case(label, "exit", context, position_id=position.id)


# --- running and reporting -------------------------------------------------------------


async def estimate_cost(catalog: ModelCatalog, models: list[str], cases: list[Case], system: str):
    """(total USD estimate or None if unknown, per-model notes)."""
    try:
        info = await catalog.models()
    except Exception:
        return None, {}
    total = 0.0
    notes = {}
    for model in models:
        m = info.get(model)
        if m is None or m.prompt_usd_per_m is None or m.completion_usd_per_m is None:
            notes[model] = "unknown model or price"
            continue
        if not m.strict_json:
            notes[model] = "no strict JSON support; results may fail"
        for case in cases:
            tokens_in = (len(system) + len(case.context)) / 3.5
            total += tokens_in * m.prompt_usd_per_m / 1e6
            total += EST_OUTPUT_TOKENS * m.completion_usd_per_m / 1e6
    return total, notes


async def run_cases(advisor: AIAdvisor, models: list[str], cases: list[Case]) -> list[dict]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def one(case: Case, model: str) -> dict[str, Any]:
        advice_type = EntryAdvice if case.kind == "entry" else ExitAdvice
        async with semaphore:
            advice, call = await advisor.run_model(
                model,
                advice_type,
                case.context,
                purpose="experiment",
                signal_id=case.signal_id,
                position_id=case.position_id,
            )
        return {
            "case": case.label,
            "model": model,
            "served_model": call.served_model,
            "ok": advice is not None,
            "action": advice.action if advice else None,
            "confidence": advice.confidence if advice else None,
            "time_horizon": advice.time_horizon if advice else None,
            "suggested_stop": advice.suggested_stop if advice else None,
            "size_adjustment": advice.suggested_size_adjustment if advice else None,
            "reasoning": advice.reasoning if advice else None,
            "key_risks": advice.key_risks if advice else None,
            "error": None if advice else call.error,
            "latency_ms": call.latency_ms,
            "tokens_in": call.tokens_in,
            "tokens_out": call.tokens_out,
            "cost_usd": call.cost_usd,
        }

    return await asyncio.gather(*(one(c, m) for c in cases for m in models))


def _clip(text: str | None, n: int) -> str:
    text = (text or "").replace("\n", " ").replace("|", "/")
    return text if len(text) <= n else text[: n - 1] + "…"


def markdown_report(rows: list[dict], cases: list[Case]) -> str:
    lines = [f"# AI model comparison, {time.strftime('%Y-%m-%d %H:%M')}", ""]
    for case in cases:
        lines += [f"## {case.label}", ""]
        lines.append(
            "| model | action | conf | horizon | stop | size | latency | cost | reasoning / error |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for r in (r for r in rows if r["case"] == case.label):
            conf = f"{r['confidence'] * 100:.0f}%" if r["confidence"] is not None else "-"
            stop = f"{r['suggested_stop']:g}" if r["suggested_stop"] else "-"
            latency = f"{r['latency_ms'] / 1000:.1f}s" if r["latency_ms"] else "-"
            cost = f"${r['cost_usd']:.4f}" if r["cost_usd"] is not None else "-"
            text = r["reasoning"] if r["ok"] else f"❌ {r['error']}"
            lines.append(
                f"| {r['model']} | {r['action'] or '-'} | {conf} | {r['time_horizon'] or '-'}"
                f" | {stop} | {r['size_adjustment'] or '-'} | {latency} | {cost}"
                f" | {_clip(text, 160)} |"
            )
        lines.append("")
    total = sum(r["cost_usd"] or 0 for r in rows)
    ok = sum(r["ok"] for r in rows)
    lines.append(f"**{ok}/{len(rows)} valid answers · total cost ${total:.4f}**")
    return "\n".join(lines)


def save_report(rows: list[dict], cases: list[Case], directory: Path = EXPERIMENTS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    md = directory / f"{stamp}.md"
    md.write_text(markdown_report(rows, cases))
    (directory / f"{stamp}.json").write_text(
        json.dumps({"cases": [c.__dict__ for c in cases], "results": rows}, indent=2, default=str)
    )
    return md


def print_summary(rows: list[dict], cases: list[Case], full: bool = False) -> None:
    for case in cases:
        print(f"\n=== {case.label} ===")
        print(f"{'model':38} {'action':20} {'conf':>5} {'horizon':9} {'secs':>5} {'cost':>9}")
        for r in (r for r in rows if r["case"] == case.label):
            conf = f"{r['confidence'] * 100:.0f}%" if r["confidence"] is not None else "-"
            secs = f"{r['latency_ms'] / 1000:.1f}" if r["latency_ms"] else "-"
            cost = f"${r['cost_usd']:.4f}" if r["cost_usd"] is not None else "-"
            action = r["action"] or "INVALID/FAILED"
            print(
                f"{r['model']:38} {action:20} {conf:>5} {r['time_horizon'] or '-':9}"
                f" {secs:>5} {cost:>9}"
            )
            detail = r["reasoning"] if r["ok"] else f"error: {r['error']}"
            print(f"    {detail if full else _clip(detail, 150)}")
            if full and r["ok"]:
                print(f"    risks: {'; '.join(r['key_risks'] or [])}")
                print(f"    suggested stop: {r['suggested_stop']:g} · size: {r['size_adjustment']}")
    total = sum(r["cost_usd"] or 0 for r in rows)
    print(f"\n{sum(r['ok'] for r in rows)}/{len(rows)} valid · total cost ${total:.4f}")


# --- CLI -----------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--live", metavar="SYMBOL", help="current live snapshot (as if a signal fired)"
    )
    source.add_argument("--signal", type=int, metavar="ID", help="replay one stored signal")
    source.add_argument("--last-signals", type=int, metavar="N", help="replay the N latest signals")
    source.add_argument(
        "--exit", type=int, metavar="POSITION_ID", help="exit decision for a position"
    )
    parser.add_argument(
        "--models", help="comma-separated OpenRouter model ids (default: ai.candidates)"
    )
    parser.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    parser.add_argument("--full", action="store_true", help="print full reasoning and risks")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    cfg = settings.strategy
    key = settings.secrets.openrouter_api_key
    if not key:
        print("OPENROUTER_API_KEY is missing in .env (get one at https://openrouter.ai/keys)")
        return 1
    models = [
        m.strip() for m in (args.models or ",".join(cfg.ai.candidates)).split(",") if m.strip()
    ]
    if not models:
        print("No models: pass --models or set ai.candidates in config.yaml")
        return 1

    repo = Repo(make_engine(PROJECT_ROOT / "data" / "bot.db"))
    if args.live:
        cases = [await live_entry_case(settings, _resolve(args.live, cfg.symbols), repo)]
    elif args.exit is not None:
        cases = [await live_exit_case(settings, args.exit, repo)]
    else:
        signals = (
            [repo.get_signal(args.signal)]
            if args.signal is not None
            else repo.recent_signals(args.last_signals)
        )
        signals = [s for s in signals if s is not None]
        if not signals:
            print("No stored signals found.")
            return 1
        cases = [signal_case(s, repo) for s in signals]

    provider = OpenRouterProvider(key.get_secret_value())
    catalog = ModelCatalog()
    advisor = AIAdvisor(provider=provider, repo=repo, strategy=cfg)
    try:
        estimate, notes = await estimate_cost(catalog, models, cases, advisor.system)
        print(f"{len(cases)} case(s) x {len(models)} model(s) = {len(cases) * len(models)} calls")
        for model, note in notes.items():
            print(f"  ⚠️ {model}: {note}")
        if estimate is not None:
            print(f"Estimated cost: ${estimate:.4f}")
            if estimate > CONFIRM_ABOVE_USD and not args.yes:
                if input("Continue? [y/N] ").strip().lower() != "y":
                    return 1
        rows = await run_cases(advisor, models, cases)
    finally:
        await provider.aclose()
        await catalog.aclose()

    print_summary(rows, cases, full=args.full)
    path = save_report(rows, cases)
    print(f"Saved: {path.relative_to(PROJECT_ROOT)} (+ .json)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
