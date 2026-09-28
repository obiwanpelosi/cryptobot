"""Asks the AI for entry/exit advice with rate limiting, validation, retries and logging.

Every public method returns None on any failure, so callers fall back to rule-only alerts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from bot.ai.base import AIError, AIProvider, AIUnavailable
from bot.ai.models import active_model, active_shadows
from bot.ai.prompts import system_prompt
from bot.ai.schema import EntryAdvice, ExitAdvice, strict_schema
from bot.settings import StrategyConfig
from bot.storage.db import Repo
from bot.storage.models import AICall

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
HOUR = 3600


class AIAdvisor:
    def __init__(
        self,
        *,
        provider: AIProvider,
        repo: Repo,
        strategy: StrategyConfig,
        on_unavailable: Callable[[str], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.provider = provider
        self.repo = repo
        self.strategy = strategy
        self.cfg = strategy.ai
        self.on_unavailable = on_unavailable
        self.clock = clock
        self.system = system_prompt(strategy)
        self.unavailable_reason: str | None = None
        self.last_skip_reason: str | None = None
        self._shadow_tasks: set[asyncio.Task] = set()

    # --- public ----------------------------------------------------------------------

    def model_for(self, role: str) -> str:
        return active_model(self.repo, self.cfg, role)

    async def analyze_entry(
        self,
        context: str,
        *,
        signal_id: int | None = None,
        purpose: str = "entry",
        role: str = "strong",
    ) -> tuple[EntryAdvice, str] | None:
        return await self._analyze(
            EntryAdvice,
            "entry_advice",
            context,
            purpose=purpose,
            role=role,
            signal_id=signal_id,
        )

    async def analyze_exit(
        self, context: str, *, position_id: int, role: str = "strong"
    ) -> tuple[ExitAdvice, str] | None:
        return await self._analyze(
            ExitAdvice,
            "exit_advice",
            context,
            purpose="exit",
            role=role,
            position_id=position_id,
        )

    async def run_model(
        self,
        model: str,
        advice_type: type[T],
        context: str,
        *,
        purpose: str = "experiment",
        signal_id: int | None = None,
        position_id: int | None = None,
        role_label: str = "primary",
    ) -> tuple[T | None, AICall]:
        """One model, with the invalid-output retry. Used by live calls and experiments."""
        name = "entry_advice" if advice_type is EntryAdvice else "exit_advice"
        return await self._call_with_retry(
            model,
            advice_type,
            name,
            context,
            purpose=purpose,
            role_label=role_label,
            signal_id=signal_id,
            position_id=position_id,
        )

    def calls_last_hour(self) -> int:
        return self.repo.ai_calls_since(int(self.clock()) - HOUR)

    async def drain(self) -> None:
        """Wait for in-flight shadow calls (tests, shutdown)."""
        if self._shadow_tasks:
            await asyncio.gather(*list(self._shadow_tasks), return_exceptions=True)

    # --- internals -------------------------------------------------------------------

    async def _analyze(
        self,
        advice_type: type[T],
        schema_name: str,
        context: str,
        *,
        purpose: str,
        role: str,
        signal_id: int | None = None,
        position_id: int | None = None,
    ) -> tuple[T, str] | None:
        self.last_skip_reason = None
        if self.unavailable_reason:
            self.last_skip_reason = f"AI unavailable: {self.unavailable_reason}"
            return None
        used = self.calls_last_hour()
        if used >= self.cfg.max_calls_per_hour:
            self.last_skip_reason = "hourly AI limit reached"
            log.info("AI skipped for %s: %s (%d calls)", purpose, self.last_skip_reason, used)
            return None

        model = self.model_for(role)
        shadows = [m for m in active_shadows(self.repo, self.cfg) if m != model]
        budget = self.cfg.max_calls_per_hour - used - 1
        for shadow in shadows[: max(budget, 0)]:
            task = asyncio.create_task(
                self._call_with_retry(
                    shadow,
                    advice_type,
                    schema_name,
                    context,
                    purpose=purpose,
                    role_label="shadow",
                    signal_id=signal_id,
                    position_id=position_id,
                )
            )
            self._shadow_tasks.add(task)
            task.add_done_callback(self._shadow_tasks.discard)

        advice, _ = await self._call_with_retry(
            model,
            advice_type,
            schema_name,
            context,
            purpose=purpose,
            role_label="primary",
            signal_id=signal_id,
            position_id=position_id,
        )
        if advice is None:
            self.last_skip_reason = self.last_skip_reason or "AI response unusable"
            return None
        return advice, model

    async def _call_with_retry(
        self,
        model: str,
        advice_type: type[T],
        schema_name: str,
        context: str,
        *,
        purpose: str,
        role_label: str,
        signal_id: int | None,
        position_id: int | None,
    ) -> tuple[T | None, AICall]:
        messages = [{"role": "user", "content": context}]
        call: AICall | None = None
        for attempt in (1, 2):
            advice, call, problem = await self._call_once(
                model,
                advice_type,
                schema_name,
                messages,
                purpose=purpose,
                role_label=role_label,
                signal_id=signal_id,
                position_id=position_id,
            )
            if advice is not None or problem is None:
                return advice, call
            if attempt == 1:
                log.warning("AI %s (%s) invalid output, retrying: %s", model, purpose, problem)
                messages = messages + [
                    {"role": "assistant", "content": call.response or ""},
                    {
                        "role": "user",
                        "content": f"Your reply was not valid: {problem}. Reply again with "
                        "JSON only, exactly matching the schema.",
                    },
                ]
        log.warning("AI %s (%s) gave invalid output twice; giving up", model, purpose)
        return None, call

    async def _call_once(
        self,
        model: str,
        advice_type: type[T],
        schema_name: str,
        messages: list[dict],
        *,
        purpose: str,
        role_label: str,
        signal_id: int | None,
        position_id: int | None,
    ) -> tuple[T | None, AICall, str | None]:
        """(advice, logged call, retryable problem or None)."""
        call = AICall(
            ts=int(self.clock()),
            provider=self.provider.name,
            model=model,
            purpose=purpose,
            role=role_label,
            prompt=json.dumps(messages),
            signal_id=signal_id,
            position_id=position_id,
            valid_json=False,
        )
        try:
            result = await self.provider.complete_json(
                model=model,
                system=self.system,
                messages=messages,
                schema_name=schema_name,
                schema=strict_schema(advice_type),
                max_tokens=self.cfg.max_output_tokens,
                timeout=self.cfg.timeout_seconds,
            )
        except AIUnavailable as exc:
            call.error = f"unavailable: {exc}"
            self.repo.add_ai_call(call)
            await self._mark_unavailable(str(exc))
            return None, call, None
        except AIError as exc:
            call.error = str(exc)
            self.repo.add_ai_call(call)
            log.warning("AI %s (%s) failed: %s", model, purpose, exc)
            return None, call, None

        call.served_model = result.served_model
        call.response = result.text
        call.latency_ms = result.latency_ms
        call.tokens_in = result.tokens_in
        call.tokens_out = result.tokens_out
        call.cost_usd = result.cost_usd
        try:
            advice = advice_type.model_validate_json(result.text or "")
        except ValidationError as exc:
            call.error = _short_validation(exc, result.finish_reason)
            self.repo.add_ai_call(call)
            return None, call, call.error
        call.valid_json = True
        self.repo.add_ai_call(call)
        log.info(
            "AI %s (%s/%s): %s conf=%.2f %sms $%s",
            model,
            purpose,
            role_label,
            advice.action,
            advice.confidence,
            result.latency_ms,
            result.cost_usd,
        )
        return advice, call, None

    async def _mark_unavailable(self, reason: str) -> None:
        first = self.unavailable_reason is None
        self.unavailable_reason = reason
        log.error("AI provider unavailable: %s", reason)
        if first and self.on_unavailable is not None:
            try:
                await self.on_unavailable(reason)
            except Exception:
                log.exception("Failed to send AI-unavailable notice")


def _short_validation(exc: ValidationError, finish_reason: str | None) -> str:
    errors: list[dict[str, Any]] = exc.errors()
    parts = [
        f"{'.'.join(str(p) for p in e.get('loc', ())) or 'json'}: {e.get('msg')}"
        for e in errors[:3]
    ]
    text = "; ".join(parts)
    if finish_reason == "length":
        text += " (output was cut off)"
    return text
