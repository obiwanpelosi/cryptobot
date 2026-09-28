"""Message formatting. Pure functions; output is Telegram HTML."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from decimal import Decimal
from html import escape

from bot.exchange.models import QUOTE_ASSET, Balance, Tick, portfolio_value_usdt
from bot.market.snapshot import MarketSnapshot
from bot.signals.rules import RuleResult
from bot.signals.sizing import Suggestion

STALE_TICK_SECONDS = 60

COMMANDS: list[tuple[str, str]] = [
    ("start", "Welcome, mode and commands"),
    ("price", "Current SOL, LINK, BTC prices and 24h change"),
    ("balance", "USDT, SOL, LINK balances and total value"),
    ("analysis", "Indicator summary, e.g. /analysis SOL"),
    ("positions", "Open positions with live P/L"),
    ("enter", "Record a position: /enter SOL 50 [price]"),
    ("close", "Close a position: /close 3 [price]"),
    ("history", "Last 20 closed trades"),
    ("ai", "AI status, models and spend"),
    ("pause", "Pause dip alerts (open positions still monitored)"),
    ("resume", "Resume dip alerts"),
]


def fmt_price(price: Decimal | float) -> str:
    return f"{price:,.2f}" if price >= 1 else f"{price:.4f}"


def plain(value: Decimal) -> str:
    """Decimal as plain digits (never scientific notation), trailing zeros removed."""
    return f"{value.normalize():f}"


def fmt_pct(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:+.{digits}f}%"


def fmt_big(value: float) -> str:
    for threshold, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= threshold:
            return f"{value / threshold:.2f}{suffix}"
    return f"{value:.0f}"


def base_asset(symbol: str) -> str:
    return symbol.removesuffix(QUOTE_ASSET)


def with_mode(text: str, mode: str) -> str:
    return f"[PAPER] {text}" if mode == "paper" else text


def start_message(mode: str, symbols: Iterable[str], paused: bool = False) -> str:
    watching = ", ".join(base_asset(s) for s in symbols)
    lines = [
        "<b>cryptobot</b> — dip-buying alerts (advisory only)",
        f"Mode: <code>{escape(mode)}</code>",
        f"Watching: {escape(watching)}",
        f"Alerts: {'⏸ paused (/resume)' if paused else '▶️ on'}",
        "",
        "<b>Commands</b>",
        *(f"/{name} — {escape(desc)}" for name, desc in COMMANDS),
    ]
    return "\n".join(lines)


def price_message(
    symbols: Iterable[str], ticks: Mapping[str, Tick], *, now_ms: int | None = None
) -> str:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    lines = ["<b>Prices</b>"]
    for symbol in symbols:
        name = escape(base_asset(symbol))
        tick = ticks.get(symbol)
        if tick is None:
            lines.append(f"{name}: n/a")
            continue
        line = f"{name}: <b>{fmt_price(tick.price)}</b> ({tick.change_24h_pct:+.1f}% 24h)"
        age_s = (now_ms - tick.event_time_ms) / 1000
        if age_s > STALE_TICK_SECONDS:
            line += f" <i>· {int(age_s)}s old</i>"
        lines.append(line)
    return "\n".join(lines)


def balance_message(balances: Mapping[str, Balance], prices: Mapping[str, Decimal]) -> str:
    total, unpriced = portfolio_value_usdt(balances, prices)
    lines = ["<b>Spot balances</b>"]
    for asset, bal in balances.items():
        name = escape(asset)
        if asset == QUOTE_ASSET:
            lines.append(f"{name}: {bal.total:,.2f}")
            continue
        amount = f"{bal.total.normalize():f}"
        price = prices.get(f"{asset}{QUOTE_ASSET}")
        value = f" ≈ {bal.total * price:,.2f} {QUOTE_ASSET}" if price is not None else ""
        lines.append(f"{name}: {amount}{value}")
    lines.append(f"<b>Total ≈ {total:,.2f} {QUOTE_ASSET}</b>")
    if unpriced:
        lines.append(f"<i>No price for {escape(', '.join(unpriced))}; not included.</i>")
    return "\n".join(lines)


HIST_ARROW = {"rising": "↑", "falling": "↓", "flat": "→", None: " "}


def _cell(value: float | None, fmt: str) -> str:
    return "-" if value is None else format(value, fmt)


def analysis_message(snap: MarketSnapshot) -> str:
    name = escape(base_asset(snap.symbol))
    lines = [
        f"<b>{name}</b> {fmt_price(snap.price)} · 1h {fmt_pct(snap.change_1h_pct)}"
        f" · 24h {fmt_pct(snap.change_24h_pct)} · 7d {fmt_pct(snap.change_7d_pct)}",
    ]
    if snap.recent_high is not None and snap.recent_low is not None:
        lines.append(
            f"{snap.high_lookback_hours}h high {fmt_price(snap.recent_high)}"
            f" ({fmt_pct(-(snap.drop_from_high_pct or 0))})"
            f" · low {fmt_price(snap.recent_low)} ({fmt_pct(snap.rise_from_low_pct)})"
        )

    rows = [f"{'TF':<4}{'RSI':>5} {'Trend':<6}{'MACD':>5}{'%B':>6}{'ATR%':>6}{'Vol':>6}"]
    for tf, t in snap.timeframes.items():
        hist_sign = "" if t.macd_hist is None else ("+" if t.macd_hist >= 0 else "-")
        macd_cell = f"{hist_sign}{HIST_ARROW[t.macd_hist_dir]}"
        vol = "-" if t.volume_ratio is None else f"{t.volume_ratio:.1f}x"
        rows.append(
            f"{tf:<4}{_cell(t.rsi, '5.1f')} {(t.trend or '-'):<6}{macd_cell:>5}"
            f"{_cell(t.bb_pct_b, '6.2f')}{_cell(t.atr_pct, '6.2f')}{vol:>6}"
        )
    lines.append("<pre>" + escape("\n".join(rows)) + "</pre>")

    if snap.supports:
        lines.append("Support: " + " · ".join(fmt_price(x) for x in snap.supports))
    if snap.resistances:
        lines.append("Resistance: " + " · ".join(fmt_price(x) for x in snap.resistances))

    if snap.btc:
        b = snap.btc
        lines.append(
            f"BTC {fmt_price(b.price)} (1h {fmt_pct(b.change_1h_pct)}, 24h"
            f" {fmt_pct(b.change_24h_pct)}) · trend 1h {b.trend_1h or '-'}"
            f" / 4h {b.trend_4h or '-'} / 1d {b.trend_1d or '-'}"
        )
    d = snap.derivatives
    if d:
        parts = []
        if d.funding_rate_pct is not None:
            parts.append(f"Funding {fmt_pct(d.funding_rate_pct, 4)}")
        if d.open_interest_usdt is not None:
            parts.append(
                f"OI {fmt_big(d.open_interest_usdt)} (24h {fmt_pct(d.oi_change_24h_pct)})"
            )
        if parts:
            lines.append(" · ".join(parts))
    if snap.sentiment:
        lines.append(
            f"Fear &amp; Greed: {snap.sentiment.fear_greed}"
            f" ({escape(snap.sentiment.classification)})"
        )
    for event in snap.macro:
        lines.append(f"📅 {event.date.isoformat()} {escape(event.name)}")

    lines.append("<i>Indicators use closed candles; the live candle isn't included.</i>")
    return "\n".join(lines)


def _fmt_check_value(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def entry_alert(
    snap: MarketSnapshot, result: RuleResult, suggestion: Suggestion | None
) -> str:
    name = escape(base_asset(snap.symbol))
    lines = []
    if result.high_risk:
        lines.append(
            f"⚠️ <b>HIGH RISK</b>: BTC {fmt_pct(result.btc_change_1h_pct)} in the last hour"
        )
    lines.append(f"📉 <b>Dip signal: {name}</b> at {fmt_price(snap.price)}")
    lines.append("")
    lines.append("<b>Why</b>")
    for c in result.checks:
        mark = "✓" if c.passed else "✗"
        lines.append(
            f"{escape(c.label)}: {_fmt_check_value(c.value)}"
            f" ({escape(c.op)} {c.threshold:g}) {mark}"
        )
    if snap.btc:
        b = snap.btc
        lines.append(
            f"BTC {fmt_price(b.price)}: 1h {fmt_pct(b.change_1h_pct)}, 24h"
            f" {fmt_pct(b.change_24h_pct)} · trend 4h {b.trend_4h or '-'} / 1d {b.trend_1d or '-'}"
        )

    lines.append("")
    lines.append("<b>Suggestion</b>")
    if suggestion is None:
        lines.append("Sizing unavailable (couldn't fetch balances). Check /balance.")
    elif suggestion.too_small:
        lines.append(f"No trade suggested: {escape(suggestion.reason or '')}")
        if suggestion.stop > 0:
            lines.append(
                f"Stop would be {fmt_price(suggestion.stop)}"
                f" (-{suggestion.stop_distance_pct:.1f}%)"
            )
    else:
        cap = ""
        if suggestion.capped_by == "max_position_pct":
            cap = " (capped at max position %)"
        elif suggestion.capped_by == "available_usdt":
            cap = " (capped at available USDT)"
        lines.append(
            f"Size: <b>{suggestion.size_usdt:,.2f} USDT</b>"
            f" ≈ {suggestion.qty.normalize():f} {name}{cap}"
        )
        lines.append(
            f"Stop: {fmt_price(suggestion.stop)} (-{suggestion.stop_distance_pct:.1f}%)"
            f" · loss if stopped: {suggestion.loss_at_stop_usdt:,.2f} USDT"
        )
        for t in suggestion.targets:
            lines.append(
                f"Target +{t.pct:g}% @ {fmt_price(t.price)} → +{t.gain_usdt:,.2f} USDT"
            )
        lines.append("<i>All P/L after fees.</i>")
    lines.append("")
    lines.append("<i>Advisory only. You place orders on Binance yourself.</i>")
    return "\n".join(lines)


ADVISORY_FOOTER = "<i>Advisory only. You place orders on Binance yourself.</i>"


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {rem // 60}m"
    return f"{rem // 60}m"


def fmt_usdt(value: float) -> str:
    return f"{value:+,.2f} USDT"


def positions_message(rows: list[dict], now: float | None = None) -> str:
    """rows: dicts with position, price (or None), pnl_usdt, pnl_pct, next_target."""
    if not rows:
        return "No open positions. Record one from an alert or with /enter SOL 50."
    now = time.time() if now is None else now
    lines = ["<b>Open positions</b>"]
    total_pnl = total_amount = 0.0
    for r in rows:
        p = r["position"]
        name = escape(base_asset(p.symbol))
        paper = " · paper" if p.is_paper else ""
        lines.append("")
        lines.append(
            f"<b>#{p.id} {name}</b> {p.amount_usdt:,.2f} USDT"
            f" @ {fmt_price(p.entry_price)} · {fmt_duration(now - p.entry_time)}{paper}"
        )
        if r["price"] is None:
            lines.append("Now: no live price yet")
        else:
            lines.append(
                f"Now {fmt_price(r['price'])} · P/L <b>{fmt_pct(r['pnl_pct'])}</b>"
                f" ({fmt_usdt(r['pnl_usdt'])})"
            )
            total_pnl += r["pnl_usdt"]
            total_amount += p.amount_usdt
        stop = "none"
        if p.stop_price:
            stop = fmt_price(p.stop_price) + (" (trailing)" if p.trailing else "")
        nxt = f"+{r['next_target']:g}%" if r["next_target"] is not None else "all hit"
        lines.append(f"Stop {stop} · next target {nxt}")
    if total_amount:
        lines.append("")
        lines.append(
            f"<b>Total open P/L: {fmt_usdt(total_pnl)}"
            f" ({fmt_pct(total_pnl / total_amount * 100)})</b>"
        )
    lines.append("<i>P/L is after fees (buy and sell).</i>")
    return "\n".join(lines)


def history_message(positions: list) -> str:
    if not positions:
        return "No closed trades yet."
    lines = [f"<b>Last {len(positions)} closed trades</b>"]
    total = 0.0
    wins = 0
    for p in positions:
        pnl = p.realised_pnl_usdt or 0.0
        total += pnl
        wins += pnl > 0
        mark = "🟢" if pnl > 0 else "🔴"
        held = fmt_duration((p.exit_time or p.entry_time) - p.entry_time)
        paper = " · paper" if p.is_paper else ""
        lines.append(
            f"{mark} #{p.id} {escape(base_asset(p.symbol))} {fmt_price(p.entry_price)}"
            f" → {fmt_price(p.exit_price or 0)} · {fmt_pct(p.realised_pnl_pct)}"
            f" ({fmt_usdt(pnl)}) · {held}{paper}"
        )
    lines.append("")
    lines.append(f"<b>Total: {fmt_usdt(total)}</b> · wins {wins}/{len(positions)}")
    return "\n".join(lines)


def position_alert_text(
    *, position, price, due, pnl_usdt, pnl_pct, next_target, suggested_trailing_stop
) -> str:
    p = position
    name = escape(base_asset(p.symbol))
    kinds = {a.kind for a in due}
    lines = []
    if "stop_hit" in kinds:
        lines.append(f"🛑 <b>{name} #{p.id} hit its stop</b> {fmt_price(p.stop_price)}")
    elif "near_stop" in kinds:
        lines.append(
            f"⚠️ <b>{name} #{p.id} is within 2% of its stop</b> {fmt_price(p.stop_price)}"
        )
    targets = [a.target_pct for a in due if a.kind == "target"]
    if targets:
        label = ", ".join(f"+{t:g}%" for t in targets)
        plural = "s" if len(targets) > 1 else ""
        lines.append(f"🎯 <b>{name} #{p.id} target{plural} {label} reached</b>")
    lines.append(
        f"Entry {fmt_price(p.entry_price)} → now {fmt_price(price)}"
        f" · P/L <b>{fmt_pct(pnl_pct)}</b> ({fmt_usdt(pnl_usdt)}) after fees"
    )
    stop = fmt_price(p.stop_price) if p.stop_price else "none"
    nxt = f"+{next_target:g}%" if next_target is not None else "all targets hit"
    lines.append(f"Stop {stop}{' (trailing)' if p.trailing else ''} · next {nxt}")
    if "trail_suggest" in kinds:
        lines.append(
            f"💡 Consider a trailing stop at {fmt_price(suggested_trailing_stop)}: it follows"
            " new highs and locks in at least breakeven."
        )
    lines.append("")
    lines.append(ADVISORY_FOOTER)
    return "\n".join(lines)


AI_FOOTER = "<i>Not financial advice. The decision is yours.</i>"
AI_ACTION_LABEL = {
    "enter": "ENTER",
    "wait": "WAIT",
    "skip": "SKIP",
    "hold": "HOLD",
    "take_profit": "TAKE PROFIT",
    "partial_take_profit": "TAKE PARTIAL PROFIT",
}


def _short_model(model: str) -> str:
    return model.split("/", 1)[-1]


def _ai_common(advice, model: str) -> list[str]:
    action = AI_ACTION_LABEL.get(advice.action, advice.action)
    lines = [
        f"🤖 <b>AI ({escape(_short_model(model))}): {action}</b>"
        f" · confidence {advice.confidence * 100:.0f}% · {escape(advice.time_horizon)}",
        escape(advice.reasoning),
    ]
    if advice.key_risks:
        lines.append("Risks: " + "; ".join(escape(r) for r in advice.key_risks[:4]))
    return lines


def _ai_stop_line(advice, rule_stop) -> str | None:
    if not advice.suggested_stop or advice.suggested_stop <= 0:
        return None
    if rule_stop is None:
        return f"AI would use stop {fmt_price(advice.suggested_stop)} (you have no stop set)"
    rule = float(rule_stop)
    if abs(advice.suggested_stop - rule) / rule < 0.002:
        return None
    ai_stop = fmt_price(advice.suggested_stop)
    return f"AI would use stop {ai_stop} (rule stop {fmt_price(rule)} kept)"


def ai_entry_section(advice, model: str, suggestion) -> str:
    lines = _ai_common(advice, model)
    rule_stop = suggestion.stop if suggestion is not None and suggestion.stop > 0 else None
    stop_line = _ai_stop_line(advice, rule_stop)
    if stop_line:
        lines.append(stop_line)
    if advice.suggested_size_adjustment == "reduce":
        lines.append("AI suggests a smaller size than the rule suggestion.")
    elif advice.suggested_size_adjustment == "increase":
        lines.append("AI suggests larger; capped at your rule size.")
    lines.append(AI_FOOTER)
    return "\n".join(lines)


def ai_exit_section(advice, model: str, rule_stop) -> str:
    lines = _ai_common(advice, model)
    stop_line = _ai_stop_line(advice, rule_stop)
    if stop_line:
        lines.append(stop_line)
    lines.append(AI_FOOTER)
    return "\n".join(lines)


def ai_skipped_line(reason: str) -> str:
    return f"<i>🤖 AI skipped: {escape(reason)}.</i>"


def ai_status_message(
    *, enabled: bool, has_key: bool, strong: str, light: str, strong_overridden: bool,
    light_overridden: bool, shadows: list[str], candidates: list[str], calls_hour: int,
    limit: int, spend_today: float, spend_month: float, unavailable: str | None,
) -> str:
    if not enabled:
        state = "off (set ai.enabled: true in config.yaml)"
    elif not has_key:
        state = "off (OPENROUTER_API_KEY missing in .env)"
    elif unavailable:
        state = f"⚠️ unavailable: {escape(unavailable)}"
    else:
        state = "on"
    lines = [
        "<b>AI</b> (OpenRouter)",
        f"Status: {state}",
        f"Strong: <code>{escape(strong)}</code>" + (" (override)" if strong_overridden else ""),
        f"Light: <code>{escape(light)}</code>" + (" (override)" if light_overridden else ""),
        "Shadow: " + (", ".join(f"<code>{escape(m)}</code>" for m in shadows) or "none"),
        f"Calls this hour: {calls_hour}/{limit}",
        f"Spend today: ${spend_today:.4f} · this month: ${spend_month:.4f}",
        "",
        "<b>Change models</b>",
        "/ai model strong &lt;model-id&gt; · /ai model light &lt;model-id&gt;",
        "/ai model strong reset",
        "/ai shadow add &lt;model-id&gt; · /ai shadow remove &lt;model-id&gt;",
        "Candidates: " + ", ".join(f"<code>{escape(m)}</code>" for m in candidates),
    ]
    return "\n".join(lines)
