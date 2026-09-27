"""Message formatting. Pure functions; output is Telegram HTML."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from decimal import Decimal
from html import escape

from bot.exchange.models import QUOTE_ASSET, Balance, Tick, portfolio_value_usdt

STALE_TICK_SECONDS = 60

COMMANDS: list[tuple[str, str]] = [
    ("start", "Welcome, mode and commands"),
    ("price", "Current SOL, LINK, BTC prices and 24h change"),
    ("balance", "USDT, SOL, LINK balances and total value"),
]


def fmt_price(price: Decimal) -> str:
    return f"{price:,.2f}" if price >= 1 else f"{price:.4f}"


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
