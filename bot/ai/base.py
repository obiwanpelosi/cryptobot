"""Provider-agnostic AI interface (requirements.md §5.9)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class AIError(Exception):
    """A failed call that may succeed later (timeout, 5xx, bad response)."""


class AIUnavailable(AIError):
    """The provider can't be used until someone fixes it (bad key, no credits)."""


@dataclass(frozen=True)
class AIResult:
    text: str
    served_model: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    latency_ms: int
    finish_reason: str | None = None


class AIProvider(Protocol):
    name: str

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
    ) -> AIResult: ...

    async def aclose(self) -> None: ...
