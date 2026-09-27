import logging

from bot.logging_setup import setup_logging


def test_httpx_request_urls_not_logged(tmp_path):
    # Telegram request URLs contain the bot token; httpx must not log them at INFO.
    setup_logging(tmp_path)
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)
