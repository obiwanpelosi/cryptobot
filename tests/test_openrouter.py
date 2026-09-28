import json

import httpx
import pytest

from bot.ai.base import AIError, AIUnavailable
from bot.ai.openrouter import OpenRouterProvider, parse_response
from bot.ai.schema import EntryAdvice, strict_schema

OK_BODY = {
    "model": "anthropic/claude-sonnet-5",
    "choices": [{"message": {"role": "assistant", "content": '{"a":1}'}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 2400, "completion_tokens": 380, "cost": 0.0086},
}


def provider(handler, **kw):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenRouterProvider("sk-or-test", http=http, retry_delay=0, **kw)


async def call(p, **overrides):
    kwargs = dict(
        model="anthropic/claude-sonnet-5",
        system="sys",
        messages=[{"role": "user", "content": "ctx"}],
        schema_name="entry_advice",
        schema=strict_schema(EntryAdvice),
        max_tokens=1500,
        timeout=5,
    )
    kwargs.update(overrides)
    return await p.complete_json(**kwargs)


async def test_request_shape_and_parsed_result():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    result = await call(provider(handler))
    body = seen["body"]
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-or-test"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert body["provider"] == {"require_parameters": True}
    assert body["usage"] == {"include": True}
    assert body["max_tokens"] == 1500
    assert result.text == '{"a":1}'
    assert result.served_model == "anthropic/claude-sonnet-5"
    assert (result.tokens_in, result.tokens_out, result.cost_usd) == (2400, 380, 0.0086)


@pytest.mark.parametrize("status", [429, 502])
async def test_transient_status_retried_once(status):
    responses = [
        httpx.Response(status, json={"error": {"message": "busy"}}),
        httpx.Response(200, json=OK_BODY),
    ]
    result = await call(provider(lambda request: responses.pop(0)))
    assert result.text == '{"a":1}' and responses == []


async def test_transient_status_gives_up_after_retry():
    with pytest.raises(AIError, match="HTTP 503"):
        await call(provider(lambda request: httpx.Response(503, json={"error": "down"})))


@pytest.mark.parametrize("status", [401, 402])
async def test_auth_and_credit_errors_are_unavailable(status):
    handler = lambda request: httpx.Response(status, json={"error": {"message": "no credits"}})  # noqa: E731
    with pytest.raises(AIUnavailable, match="no credits"):
        await call(provider(handler))


async def test_timeout_maps_to_ai_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(AIError, match="timeout"):
        await call(provider(handler))


def test_parse_response_edge_cases():
    with pytest.raises(AIError, match="refused"):
        parse_response({"choices": [{"message": {"refusal": "no"}}]}, 1)
    with pytest.raises(AIError, match="no choices"):
        parse_response({"choices": []}, 1)
    with pytest.raises(AIError, match="provider error"):
        parse_response({"error": {"message": "bad model"}}, 1)
    parts = {"choices": [{"message": {"content": [{"type": "text", "text": "{}"}]}}]}
    assert parse_response(parts, 1).text == "{}"
