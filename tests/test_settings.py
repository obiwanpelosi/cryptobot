import pytest
import yaml
from pydantic import ValidationError

from bot.settings import PROJECT_ROOT, Secrets, load_strategy

ENV_VARS = [
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_SECRET_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_repo_config_loads():
    cfg = load_strategy(PROJECT_ROOT / "config.yaml")
    assert cfg.symbols == ["SOLUSDT", "LINKUSDT"]
    assert cfg.mode == "paper"
    assert cfg.exit_alerts.profit_targets_pct == [10, 15, 20]
    assert cfg.ai.enabled is False


def test_bad_mode_rejected(tmp_path):
    raw = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text())
    raw["mode"] = "yolo"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError):
        load_strategy(path)


def test_chat_ids_parsed_from_comma_list(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "123, 456,789")
    assert Secrets(_env_file=None).telegram_allowed_chat_ids == [123, 456, 789]


def test_chat_ids_default_empty():
    assert Secrets(_env_file=None).telegram_allowed_chat_ids == []


@pytest.mark.parametrize("name", ["BINANCE_API_SECRET", "BINANCE_SECRET_KEY"])
def test_binance_secret_accepts_both_names(monkeypatch, name):
    monkeypatch.setenv(name, "s3cret")
    secrets = Secrets(_env_file=None)
    assert secrets.binance_api_secret.get_secret_value() == "s3cret"
    assert "s3cret" not in repr(secrets)


def test_env_file_is_read(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=abc\nTELEGRAM_BOT_TOKEN=\n")
    secrets = Secrets(_env_file=env)
    assert secrets.binance_api_key.get_secret_value() == "abc"
    assert secrets.telegram_bot_token is None
