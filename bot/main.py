"""Entry point: `uv run python -m bot.main`."""

from __future__ import annotations

import asyncio
import logging

from bot.logging_setup import setup_logging
from bot.settings import PROJECT_ROOT, load_settings

log = logging.getLogger("bot")


def _present(value: object) -> str:
    return "present" if value else "missing"


async def main() -> None:
    setup_logging(PROJECT_ROOT / "logs")
    settings = load_settings()
    s, cfg = settings.secrets, settings.strategy

    log.info("Starting cryptobot | mode=%s symbols=%s", cfg.mode, cfg.symbols)
    log.info(
        "binance keys: %s | telegram token: %s (%d allowed chats) | anthropic key: %s",
        _present(s.binance_api_key and s.binance_api_secret),
        _present(s.telegram_bot_token),
        len(s.telegram_allowed_chat_ids),
        _present(s.anthropic_api_key),
    )
    # Phase 1 replaces this with the long-running exchange/telegram tasks.
    log.info("Setup check complete, exiting.")


if __name__ == "__main__":
    asyncio.run(main())
