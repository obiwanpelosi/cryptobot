"""Load and validate secrets (.env) and strategy parameters (config.yaml)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import AliasChoices, BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Secrets(BaseSettings):
    """Secrets read from the environment / .env. Values never appear in reprs or logs."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    binance_api_key: SecretStr | None = None
    binance_api_secret: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("BINANCE_API_SECRET", "BINANCE_SECRET_KEY"),
    )
    telegram_bot_token: SecretStr | None = None
    telegram_allowed_chat_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None

    @field_validator("telegram_allowed_chat_ids", mode="before")
    @classmethod
    def _split_chat_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.split(",") if part.strip()]
        if isinstance(value, int):
            return [value]
        return value


class DipRules(BaseModel):
    min_drop_from_high_pct: float = Field(gt=0)
    high_lookback_hours: int = Field(gt=0)
    rsi_1h_max: float = Field(gt=0, lt=100)
    require_lower_bollinger_4h: bool = False
    btc_crash_guard_pct_1h: float = Field(gt=0)
    cooldown_minutes_per_symbol: int = Field(ge=0)


class ExitAlerts(BaseModel):
    profit_targets_pct: list[float] = Field(min_length=1)
    stop_atr_multiplier: float = Field(gt=0)
    trailing_after_pct: float | None = Field(default=None, gt=0)


class Sizing(BaseModel):
    risk_per_trade_pct: float = Field(gt=0, le=100)
    max_position_pct: float = Field(gt=0, le=100)
    min_order_usdt: float = Field(ge=0)


class Fees(BaseModel):
    taker_pct: float = Field(ge=0)


class AIConfig(BaseModel):
    enabled: bool = False
    provider: str = "anthropic"
    light_model: str
    strong_model: str
    max_calls_per_hour: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)


class StrategyConfig(BaseModel):
    symbols: list[str] = Field(min_length=1)
    context_symbols: list[str] = Field(default_factory=list)
    mode: Literal["paper", "live-advisory", "live-execution"] = "paper"
    timeframes: list[str] = Field(min_length=1)
    history_candles: int = Field(gt=0)
    dip_rules: DipRules
    exit_alerts: ExitAlerts
    sizing: Sizing
    fees: Fees
    ai: AIConfig


class Settings(BaseModel):
    secrets: Secrets
    strategy: StrategyConfig


def load_strategy(config_path: str | Path = PROJECT_ROOT / "config.yaml") -> StrategyConfig:
    with open(config_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return StrategyConfig.model_validate(raw)


def load_settings(
    config_path: str | Path = PROJECT_ROOT / "config.yaml",
    env_file: str | Path | None = PROJECT_ROOT / ".env",
) -> Settings:
    """Load everything once at startup; raises pydantic.ValidationError on bad config."""
    return Settings(
        secrets=Secrets(_env_file=env_file),
        strategy=load_strategy(config_path),
    )
