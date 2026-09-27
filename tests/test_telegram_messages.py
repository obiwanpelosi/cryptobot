from decimal import Decimal as D

from bot.exchange.models import Balance, Tick
from bot.telegram.messages import balance_message, price_message, start_message, with_mode


def test_with_mode_prefixes_only_paper():
    assert with_mode("hi", "paper") == "[PAPER] hi"
    assert with_mode("hi", "live-advisory") == "hi"
    assert with_mode("hi", "live-execution") == "hi"


def test_start_message_lists_commands_and_mode():
    text = start_message("paper", ["SOLUSDT", "LINKUSDT"])
    assert "paper" in text
    assert "SOL, LINK" in text
    for cmd in ("/start", "/price", "/balance"):
        assert cmd in text


def test_price_message_formats_and_handles_missing():
    ticks = {
        "SOLUSDT": Tick("SOLUSDT", D("123.456"), D("-3.14"), 1_000),
        "LINKUSDT": Tick("LINKUSDT", D("0.5"), D("1"), 1_000),
    }
    text = price_message(["SOLUSDT", "LINKUSDT", "BTCUSDT"], ticks, now_ms=2_000)
    assert "SOL: <b>123.46</b> (-3.1% 24h)" in text
    assert "LINK: <b>0.5000</b> (+1.0% 24h)" in text
    assert "BTC: n/a" in text
    assert "old" not in text


def test_price_message_flags_old_ticks():
    ticks = {"SOLUSDT": Tick("SOLUSDT", D("100"), D("0"), 0)}
    text = price_message(["SOLUSDT"], ticks, now_ms=120_000)
    assert "120s old" in text


def test_balance_message_totals():
    balances = {
        "USDT": Balance("USDT", D("100"), D("0")),
        "SOL": Balance("SOL", D("1.5"), D("0")),
        "LINK": Balance("LINK", D("0"), D("0")),
    }
    text = balance_message(balances, {"SOLUSDT": D("100"), "LINKUSDT": D("15")})
    assert "USDT: 100.00" in text
    assert "SOL: 1.5 ≈ 150.00 USDT" in text
    assert "Total ≈ 250.00 USDT" in text


def test_balance_message_escapes_and_flags_unpriced():
    balances = {"<X>": Balance("<X>", D("1"), D("0"))}
    text = balance_message(balances, {})
    assert "<X>" not in text
    assert "&lt;X&gt;" in text
    assert "No price for" in text
