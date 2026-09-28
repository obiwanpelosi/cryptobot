"""OpenRouter chat-completions client (one API key, many models).

https://openrouter.ai/docs — OpenAI-compatible `POST /api/v1/chat/completions`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from bot.ai.base import AIError, AIResult, AIUnavailable

log = logging.getLogger(__name__)

BASE_URL = "https://openrouter.ai/api/v1"
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
UNAVAILABLE_STATUSES = {401, 402, 403}


class OpenRouterProvider:
    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        *,
        http: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
        max_retries: int = 1,
        retry_delay: float = 2.0,
    ):
        self._api_key = api_key
        self._http = http or httpx.AsyncClient()
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._retry_delay = retry_delay

    async def aclose(self) -> None:
        await self._http.aclose()

    def build_body(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, str]],
        schema_name: str,
        schema: dict[str, Any],
        max_tokens: int,
    ) -> dict[str, Any]:
        return {
            "model": model,
            "messages": [{"role": "system", "content": system}, *messages],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
            # Only route to providers that honour response_format for this model.
            "provider": {"require_parameters": True},
            "usage": {"include": True},  # tokens + actual USD cost in the response
            "max_tokens": max_tokens,
        }

    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, str]],
        schema_name: str,
        schema: dict[str, Any],
        max_tokens: int,
        timeout: float,
    ) -> AIResult:
        body = self.build_body(
            model=model,
            system=system,
            messages=messages,
            schema_name=schema_name,
            schema=schema,
            max_tokens=max_tokens,
        )
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "X-Title": "cryptobot",
        }
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                response = await self._http.post(
                    f"{self._base_url}/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=timeout,
                )
            except httpx.TimeoutException as exc:
                raise AIError(f"timeout after {timeout:.0f}s") from exc
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    attempt += 1
                    await asyncio.sleep(self._retry_delay)
                    continue
                raise AIError(f"network error: {exc}") from exc

            latency_ms = int((time.monotonic() - started) * 1000)
            if response.status_code in UNAVAILABLE_STATUSES:
                raise AIUnavailable(f"HTTP {response.status_code}: {_error_text(response)}")
            if response.status_code in RETRY_STATUSES and attempt < self._max_retries:
                attempt += 1
                log.warning("OpenRouter HTTP %s, retrying", response.status_code)
                await asyncio.sleep(self._retry_delay * attempt)
                continue
            if response.status_code != 200:
                raise AIError(f"HTTP {response.status_code}: {_error_text(response)}")
            return parse_response(response.json(), latency_ms)


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:200]
    return str(data)[:200]


def parse_response(data: dict[str, Any], latency_ms: int) -> AIResult:
    if "error" in data and not data.get("choices"):
        err = data["error"]
        message = err.get("message") if isinstance(err, dict) else err
        raise AIError(f"provider error: {message}")
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIError("response has no choices") from exc
    message = choice.get("message") or {}
    if message.get("refusal"):
        raise AIError(f"model refused: {message['refusal']}")
    content = message.get("content")
    if isinstance(content, list):  # some providers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    usage = data.get("usage") or {}
    return AIResult(
        text=content or "",
        served_model=data.get("model"),
        tokens_in=usage.get("prompt_tokens"),
        tokens_out=usage.get("completion_tokens"),
        cost_usd=usage.get("cost"),
        latency_ms=latency_ms,
        finish_reason=choice.get("finish_reason"),
    )
