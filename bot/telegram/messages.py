"""Message formatting. Pure functions; output is Telegram HTML."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from decimal import Decimal
from html import escape

from bot.exchange.models import QUOTE_ASSET, Balance, Tick, portfolio_value_usdt
from bot.market.snapshot import MarketSnapshot

STALE_TICK_SECONDS = 60

COMMANDS: list[tuple[str, str]] = [
    ("start", "Welcome, mode and commands"),
    ("price", "Current SOL, LINK, BTC prices and 24h change"),
    ("balance", "USDT, SOL, LINK balances and total value"),
    ("analysis", "Indicator summary, e.g. /analysis SOL"),
]


def fmt_price(price: Decimal | float) -> str:
    return f"{price:,.2f}" if price >= 1 else f"{price:.4f}"


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


def start_message(mode: str, symbols: Iterable[str]) -> str:
    watching = ", ".join(base_asset(s) for s in symbols)
    lines = [
        "<b>cryptobot</b> — dip-buying alerts (advisory only)",
        f"Mode: <code>{escape(mode)}</code>",
        f"Watching: {escape(watching)}",
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
