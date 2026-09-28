"""OpenRouter model catalog checks and runtime model selection."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from bot.ai.openrouter import BASE_URL
from bot.settings import AIConfig
from bot.storage.db import Repo

log = logging.getLogger(__name__)

CATALOG_TTL_SECONDS = 3600
ROLES = ("strong", "light")
SHADOW_KEY = "ai_shadow_models"


@dataclass(frozen=True)
class ModelInfo:
    id: str
    strict_json: bool
    prompt_usd_per_m: float | None
    completion_usd_per_m: float | None


def _per_million(pricing: dict[str, Any], key: str) -> float | None:
    try:
        return float(pricing[key]) * 1_000_000
    except (KeyError, TypeError, ValueError):
        return None


def parse_catalog(data: dict[str, Any]) -> dict[str, ModelInfo]:
    out = {}
    for m in data.get("data", []):
        pricing = m.get("pricing") or {}
        params = set(m.get("supported_parameters") or [])
        out[m["id"]] = ModelInfo(
            id=m["id"],
            strict_json="structured_outputs" in params,
            prompt_usd_per_m=_per_million(pricing, "prompt"),
            completion_usd_per_m=_per_million(pricing, "completion"),
        )
    return out


class ModelCatalog:
    """OpenRouter's public model list (no key needed), cached for an hour."""

    def __init__(self, http: httpx.AsyncClient | None = None, base_url: str = BASE_URL):
        self._http = http or httpx.AsyncClient()
        self._url = f"{base_url.rstrip('/')}/models"
        self._models: dict[str, ModelInfo] | None = None
        self._fetched_at = 0.0

    async def models(self) -> dict[str, ModelInfo]:
        if self._models is None or time.monotonic() - self._fetched_at > CATALOG_TTL_SECONDS:
            response = await self._http.get(self._url, timeout=20)
            response.raise_for_status()
            self._models = parse_catalog(response.json())
            self._fetched_at = time.monotonic()
        return self._models

    async def validate(self, model_id: str) -> str | None:
        """None if usable; otherwise a human-readable reason."""
        try:
            models = await self.models()
        except Exception as exc:
            log.warning("Could not fetch OpenRouter model list: %s", exc)
            return None  # don't block on a catalog outage
        info = models.get(model_id)
        if info is None:
            return f"{model_id} is not an OpenRouter model id (see openrouter.ai/models)"
        if not info.strict_json:
            return f"{model_id} doesn't support strict JSON (structured outputs)"
        return None

    async def aclose(self) -> None:
        await self._http.aclose()


# --- runtime selection (overrides persisted in bot_state) ------------------------------


def _key(role: str) -> str:
    return f"ai_model_{role}"


def active_model(repo: Repo, cfg: AIConfig, role: str) -> str:
    override = repo.get_state(_key(role))
    return override or (cfg.strong_model if role == "strong" else cfg.light_model)


def set_model_override(repo: Repo, role: str, model_id: str | None) -> None:
    repo.set_state(_key(role), model_id or "")


def active_shadows(repo: Repo, cfg: AIConfig) -> list[str]:
    raw = repo.get_state(SHADOW_KEY)
    return json.loads(raw) if raw else list(cfg.shadow_models)


def set_shadows(repo: Repo, models: list[str]) -> None:
    repo.set_state(SHADOW_KEY, json.dumps(sorted(set(models))))
