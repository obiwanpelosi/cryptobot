"""Shared fakes for AI tests (no network)."""

import json

from bot.ai.base import AIError, AIResult, AIUnavailable

VALID_ENTRY = {
    "action": "enter",
    "confidence": 0.62,
    "reasoning": "Oversold on 1h with BTC stable. Risk is defined by the stop.",
    "key_risks": ["BTC could break down", "Low volume bounce"],
    "suggested_stop": 115.2,
    "suggested_size_adjustment": "reduce",
    "time_horizon": "1-3 days",
}
VALID_EXIT = {**VALID_ENTRY, "action": "hold", "suggested_size_adjustment": "none"}


def result(payload, *, model="served/model", cost=0.0012, text=None):
    return AIResult(
        text=text if text is not None else json.dumps(payload),
        served_model=model,
        tokens_in=2500,
        tokens_out=400,
        cost_usd=cost,
        latency_ms=1234,
        finish_reason="stop",
    )


class FakeProvider:
    """Scripted responses per model: a list consumed in order; a default when exhausted."""

    name = "openrouter"

    def __init__(self, script=None, default=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.default = default if default is not None else result(VALID_ENTRY)
        self.calls = []

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        queue = self.script.get(kwargs["model"])
        item = queue.pop(0) if queue else self.default
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        pass


__all__ = ["AIError", "AIUnavailable", "FakeProvider", "VALID_ENTRY", "VALID_EXIT", "result"]
