"""AI output models (requirements.md §5.9) and strict JSON-schema generation."""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SizeAdjustment = Literal["none", "reduce", "increase"]
TimeHorizon = Literal["hours", "1-3 days", "3-7 days"]


class _Advice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confidence: float = Field(ge=0.0, le=1.0, description="0 to 1")
    reasoning: str = Field(description="2-4 sentences")
    key_risks: list[str]
    suggested_stop: float = Field(ge=0.0, description="Stop price you would use; 0 if none")
    suggested_size_adjustment: SizeAdjustment
    time_horizon: TimeHorizon


class EntryAdvice(_Advice):
    action: Literal["enter", "wait", "skip"]


class ExitAdvice(_Advice):
    action: Literal["hold", "take_profit", "partial_take_profit"]


# Keywords some providers reject in strict mode; pydantic still enforces them after parsing.
_STRIP_KEYS = {"title", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "default"}


def _strictify(node: Any) -> Any:
    if isinstance(node, dict):
        out = {k: _strictify(v) for k, v in node.items() if k not in _STRIP_KEYS}
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out
    if isinstance(node, list):
        return [_strictify(v) for v in node]
    return node


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for structured outputs: no extra keys, every field required."""
    return _strictify(copy.deepcopy(model.model_json_schema()))
