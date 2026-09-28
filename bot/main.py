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
from bot.exchange.models import QUOTE_ASSET, Candle, portfolio_value_usdt
from bot.exchange.streams import PriceStream
from bot.logging_setup import setup_logging
from bot.market.candles import CandleStore
from bot.market.context import MarketContext
from bot.market.snapshot import SnapshotBuilder
from bot.positions.tracker import PositionTracker
from bot.settings import PROJECT_ROOT, Settings, load_settings
from bot.signals.engine import SignalEngine
from bot.signals.sizing import SymbolFilters
from bot.storage.db import Repo, make_engine
from bot.telegram.alerts import entry_keyboard
from bot.telegram.deps import Deps
from bot.telegram.handlers import build_application, polling_error
from bot.telegram.messages import base_asset, fmt_price
from bot.telegram.notifier import Notifier, NullNotifier
from bot.telegram.positions import render_position_alert

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


class CandleSync:
    """Feeds closed candles into the store and backfills after reconnects or gaps."""

    def __init__(self, store: CandleStore, client: BinanceClient):
        self.store = store
        self.client = client
        self.connects = 0
        self.engine: SignalEngine | None = None
        self._task: asyncio.Task | None = None

    def on_candle(self, symbol: str, tf: str, candle: Candle) -> None:
        if self.store.add(symbol, tf, candle):
            self.schedule_backfill()
        if self.engine is not None:
            self.engine.on_candle_closed(symbol, tf)

    def on_connect(self) -> None:
        self.connects += 1
        if self.connects > 1:  # the first connect follows a fresh load
            self.schedule_backfill()

    def schedule_backfill(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._backfill(), name="backfill")

    async def _backfill(self) -> None:
        """Backfill, then treat candles that arrived via REST like live closes."""
        advanced = await self.store.backfill(self.client)
        if self.engine is not None:
            for symbol, tf in sorted(advanced):
                self.engine.on_candle_closed(symbol, tf)


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
    await app.updater.start_polling(drop_pending_updates=True, error_callback=polling_error)
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
        symbol_info = await client.check_symbols(all_symbols)
        filters = {
            sym: SymbolFilters.from_exchange_info(symbol_info[sym]) for sym in cfg.symbols
        }
        repo = Repo(make_engine(PROJECT_ROOT / "data" / "bot.db"))

        store = CandleStore(all_symbols, cfg.timeframes, cfg.history_candles)
        await store.load(client)
        market = MarketContext(client, cfg.symbols, cfg.macro_events)
        await market.refresh()

        tracker = PositionTracker(
            repo=repo,
            strategy=cfg,
            notifier=NullNotifier(),  # replaced once Telegram is up, before the stream runs
            tick_size=lambda sym: float(filters[sym].tick_size) if sym in filters else 0.0,
            render=render_position_alert,
        )
        tracker.load()

        sync = CandleSync(store, client)
        bsm = BinanceSocketManager(client.raw)
        stream = PriceStream(
            all_symbols,
            bsm.multiplex_socket,
            on_tick=tracker.on_tick,
            kline_intervals=cfg.timeframes,
            on_candle=sync.on_candle,
            on_connect=sync.on_connect,
        )
        stream.seed(await client.get_24h_tickers(all_symbols))
        snapshots = SnapshotBuilder(
            store,
            market,
            stream.ticks,
            high_lookback_hours=cfg.dip_rules.high_lookback_hours,
            btc_symbol=cfg.context_symbols[0] if cfg.context_symbols else None,
        )

        def atr_4h(symbol: str) -> float | None:
            base = snapshots.base_indicators(symbol, "4h")
            return base.atr if base else None

        tracker.atr_4h = atr_4h

        deps = Deps(
            client=client,
            stream=stream,
            settings=settings,
            snapshots=snapshots,
            repo=repo,
            filters=filters,
            tracker=tracker,
        )
        app = await start_telegram(settings, deps)
        if app is not None:
            notifier = Notifier(app.bot, s.telegram_allowed_chat_ids, cfg.mode)
            deps.notifier = notifier
        tracker.notifier = notifier
        sync.engine = SignalEngine(
            strategy=cfg,
            snapshots=snapshots,
            repo=repo,
            balances=client,
            filters=filters,
            notifier=notifier,
            keyboard=entry_keyboard,
        )
        watching = ", ".join(base_asset(sym) for sym in all_symbols)
        await notifier.send(f"🟢 cryptobot started. Mode: {cfg.mode}. Watching {watching}.")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        tasks = [
            asyncio.create_task(stream.run(), name="price-stream"),
            asyncio.create_task(reporter(client, stream, assets, notifier), name="reporter"),
            asyncio.create_task(market.run(), name="market-context"),
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
