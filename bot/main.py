"""Entry point: `uv run python -m bot.main`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from html import escape

from binance import BinanceSocketManager
from telegram.ext import Application

from bot.exchange.client import BinanceClient
from bot.exchange.models import QUOTE_ASSET, portfolio_value_usdt
from bot.exchange.streams import PriceStream
from bot.logging_setup import setup_logging
from bot.settings import PROJECT_ROOT, Settings, load_settings
from bot.telegram.handlers import Deps, build_application
from bot.telegram.messages import base_asset, fmt_price
from bot.telegram.notifier import Notifier, NullNotifier

log = logging.getLogger("bot")

PRICE_REPORT_SECONDS = 10
BALANCE_REPORT_SECONDS = 60


def _present(value: object) -> str:
    return "present" if value else "missing"


def format_prices(stream: PriceStream) -> str:
    parts = []
    for symbol in stream.symbols:
        tick = stream.ticks.get(symbol)
        if tick is None:
            parts.append(f"{base_asset(symbol)} n/a")
        else:
            parts.append(
                f"{base_asset(symbol)} {fmt_price(tick.price)} ({tick.change_24h_pct:+.1f}%)"
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


class StaleMonitor:
    """Turns the stream's stale flag into one-shot 'stale' / 'recovered' transitions."""

    def __init__(self) -> None:
        self.stale = False

    def update(self, is_stale: bool) -> str | None:
        if is_stale and not self.stale:
            self.stale = True
            return "stale"
        if not is_stale and self.stale:
            self.stale = False
            return "recovered"
        return None


async def check_stale(stream: PriceStream, monitor: StaleMonitor, notifier) -> None:
    transition = monitor.update(stream.is_stale())
    if transition == "stale":
        log.warning("Price data is stale: no stream message for over 5 minutes")
        await notifier.send("⚠️ No price data for 5+ minutes. Stream is reconnecting.")
    elif transition == "recovered":
        log.info("Price stream recovered")
        await notifier.send("✅ Price stream recovered.")


async def reporter(
    client: BinanceClient, stream: PriceStream, assets: list[str], notifier
) -> None:
    elapsed = 0
    monitor = StaleMonitor()
    while True:
        log.info("Prices: %s", format_prices(stream))
        if elapsed % BALANCE_REPORT_SECONDS == 0:
            try:
                await report_balances(client, stream, assets)
            except Exception:
                log.exception("Failed to fetch balances")
        await check_stale(stream, monitor, notifier)
        await asyncio.sleep(PRICE_REPORT_SECONDS)
        elapsed += PRICE_REPORT_SECONDS


async def start_telegram(settings: Settings, deps: Deps) -> Application | None:
    s = settings.secrets
    if not s.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN not set; running terminal-only")
        return None
    if not s.telegram_allowed_chat_ids:
        log.warning("TELEGRAM_ALLOWED_CHAT_IDS is empty; the bot will ignore every message")
    app = build_application(s.telegram_bot_token.get_secret_value(), deps)
    await app.initialize()
    if app.post_init:
        await app.post_init(app)
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram bot @%s polling", app.bot.username)
    return app


async def stop_telegram(app: Application | None) -> None:
    if app is None:
        return
    with contextlib.suppress(Exception):
        if app.updater and app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()


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
    assets = [QUOTE_ASSET] + [base_asset(sym) for sym in cfg.symbols]

    client = await BinanceClient.create(s)
    app: Application | None = None
    notifier: Notifier | NullNotifier = NullNotifier()
    crash: BaseException | None = None
    try:
        await client.check_permissions()
        await client.check_symbols(all_symbols)

        bsm = BinanceSocketManager(client.raw)
        stream = PriceStream(all_symbols, bsm.multiplex_socket)
        stream.seed(await client.get_24h_tickers(all_symbols))

        deps = Deps(client=client, stream=stream, settings=settings)
        app = await start_telegram(settings, deps)
        if app is not None:
            notifier = Notifier(app.bot, s.telegram_allowed_chat_ids, cfg.mode)
            deps.notifier = notifier
        watching = ", ".join(base_asset(sym) for sym in all_symbols)
        await notifier.send(f"🟢 cryptobot started. Mode: {cfg.mode}. Watching {watching}.")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        tasks = [
            asyncio.create_task(stream.run(), name="price-stream"),
            asyncio.create_task(reporter(client, stream, assets, notifier), name="reporter"),
        ]
        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)

        for task in done:
            if task is not stopper and not task.cancelled() and task.exception():
                crash = task.exception()

        log.info("Shutting down...")
        for task in [*tasks, stopper]:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    except Exception as exc:
        crash = exc
    finally:
        if crash is not None:
            reason = f"{type(crash).__name__}: {escape(str(crash))}"
            await notifier.send(f"🔴 cryptobot crashed: {reason}")
        else:
            await notifier.send("🔴 cryptobot stopping.")
        await stop_telegram(app)
        await client.close()
        log.info("Stopped.")
    if crash is not None:
        raise crash


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
