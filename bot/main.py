"""Entry point: `uv run python -m bot.main`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from decimal import Decimal

from binance import BinanceSocketManager

from bot.exchange.client import BinanceClient
from bot.exchange.models import QUOTE_ASSET, portfolio_value_usdt
from bot.exchange.streams import PriceStream
from bot.logging_setup import setup_logging
from bot.settings import PROJECT_ROOT, Settings, load_settings

log = logging.getLogger("bot")

PRICE_REPORT_SECONDS = 10
BALANCE_REPORT_SECONDS = 60


def _present(value: object) -> str:
    return "present" if value else "missing"


def _fmt_price(price: Decimal) -> str:
    return f"{price:,.2f}" if price >= 1 else f"{price:.4f}"


def _base_asset(symbol: str) -> str:
    return symbol.removesuffix(QUOTE_ASSET)


def format_prices(stream: PriceStream) -> str:
    parts = []
    for symbol in stream.symbols:
        tick = stream.ticks.get(symbol)
        if tick is None:
            parts.append(f"{_base_asset(symbol)} n/a")
        else:
            parts.append(
                f"{_base_asset(symbol)} {_fmt_price(tick.price)} ({tick.change_24h_pct:+.1f}%)"
            )
    return " | ".join(parts)


async def report_balances(client: BinanceClient, stream: PriceStream, assets: list[str]) -> None:
    balances = await client.get_balances(assets)
    prices = {s: t.price for s, t in stream.ticks.items()}
    total, unpriced = portfolio_value_usdt(balances, prices)
    parts = []
    for asset, bal in balances.items():
        if asset == QUOTE_ASSET:
            parts.append(f"{asset} {bal.total:,.2f}")
        else:
            price = prices.get(f"{asset}{QUOTE_ASSET}")
            value = f" ({bal.total * price:,.2f})" if price is not None else ""
            parts.append(f"{asset} {bal.total.normalize():f}{value}")
    line = " | ".join(parts) + f" | total ≈ {total:,.2f} {QUOTE_ASSET}"
    if unpriced:
        line += f" (no price for {', '.join(unpriced)})"
    log.info("Balances: %s", line)


async def reporter(client: BinanceClient, stream: PriceStream, assets: list[str]) -> None:
    elapsed = 0
    stale_warned = False
    while True:
        log.info("Prices: %s", format_prices(stream))
        if elapsed % BALANCE_REPORT_SECONDS == 0:
            try:
                await report_balances(client, stream, assets)
            except Exception:
                log.exception("Failed to fetch balances")
        if stream.is_stale():
            if not stale_warned:
                log.warning("Price data is stale: no stream message for over 5 minutes")
                stale_warned = True
        elif stale_warned:
            log.info("Price stream recovered")
            stale_warned = False
        await asyncio.sleep(PRICE_REPORT_SECONDS)
        elapsed += PRICE_REPORT_SECONDS


async def run(settings: Settings) -> None:
    s, cfg = settings.secrets, settings.strategy
    log.info("Starting cryptobot | mode=%s symbols=%s", cfg.mode, cfg.symbols)
    log.info(
        "binance keys: %s | telegram token: %s (%d allowed chats) | anthropic key: %s",
        _present(s.binance_api_key and s.binance_api_secret),
        _present(s.telegram_bot_token),
        len(s.telegram_allowed_chat_ids),
        _present(s.anthropic_api_key),
    )

    all_symbols = list(dict.fromkeys(cfg.symbols + cfg.context_symbols))
    assets = [QUOTE_ASSET] + [_base_asset(sym) for sym in cfg.symbols]

    client = await BinanceClient.create(s)
    try:
        await client.check_permissions()
        await client.check_symbols(all_symbols)

        bsm = BinanceSocketManager(client.raw)
        stream = PriceStream(all_symbols, bsm.multiplex_socket)
        stream.seed(await client.get_24h_tickers(all_symbols))

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        tasks = [
            asyncio.create_task(stream.run(), name="price-stream"),
            asyncio.create_task(reporter(client, stream, assets), name="reporter"),
        ]
        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)

        log.info("Shutting down...")
        for task in [*tasks, stopper]:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:  # surface a crash in a worker task
            if task is not stopper and not task.cancelled() and task.exception():
                raise task.exception()
    finally:
        await client.close()
        log.info("Stopped.")


def main() -> None:
    setup_logging(PROJECT_ROOT / "logs")
    try:
        asyncio.run(run(load_settings()))
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("Fatal error")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
