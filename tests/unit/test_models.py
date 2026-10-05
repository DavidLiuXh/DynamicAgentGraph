import asyncio
from types import SimpleNamespace

import pytest

from dynamic_graph.models.adapters import LangChainModelClient
from dynamic_graph.models.client import ModelCallError, ModelRequest

REQUEST = ModelRequest(
    "worker",
    "system",
    "task",
    {},
    {"type": "object", "properties": {}, "additionalProperties": False},
)


class ProviderError(Exception):
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body or {}


class FakeProvider:
    max_retries = 0

    def __init__(self, response=None, exception=None):
        self.response, self.exception = response, exception
        self.calls = 0
        self.schema = None

    def with_structured_output(self, schema, **kwargs):
        self.schema = schema
        self.configuration = kwargs
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if self.exception:
            raise self.exception
        return self.response


def response(**changes):
    raw = SimpleNamespace(
        response_metadata={"finish_reason": "tool_calls"},
        additional_kwargs={},
        usage_metadata=None,
        id="provider-id",
    )
    data = {"parsed": {}, "raw": raw, "parsing_error": None}
    data.update(changes)
    return data


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "MODEL_AUTH_FAILED", False),
        (403, "MODEL_PERMISSION_DENIED", False),
        (402, "MODEL_QUOTA_EXHAUSTED", False),
        (429, "MODEL_RATE_LIMITED", True),
        (500, "MODEL_UNAVAILABLE", True),
        (400, "MODEL_STRUCTURED_OUTPUT_UNSUPPORTED", False),
    ],
)
async def test_error_mapping_single_request(status, code, retryable):
    provider = FakeProvider(exception=ProviderError(status))
    client = LangChainModelClient(chat_model=provider)
    with pytest.raises(ModelCallError) as caught:
        await client.generate(REQUEST)
    assert caught.value.code == code and caught.value.retryable == retryable
    assert provider.calls == 1


async def test_success_unknown_usage_and_no_business_tool_dispatch():
    provider = FakeProvider(response())
    result = await LangChainModelClient(chat_model=provider).generate(REQUEST)
    assert result.payload == {} and result.usage["input_tokens"] is None
    assert result.provider_request_id == "provider-id"
    assert provider.configuration["method"] == "function_calling" and provider.calls == 1


@pytest.mark.parametrize(
    "case,expected",
    [
        ("refusal", "MODEL_REFUSED"),
        ("truncated", "MODEL_RESPONSE_TRUNCATED"),
        ("parse", "MODEL_RESPONSE_INVALID"),
    ],
)
async def test_response_failure_classification(case, expected):
    data = response()
    if case == "refusal":
        data["raw"].additional_kwargs["refusal"] = "refused"
    elif case == "truncated":
        data["raw"].response_metadata["finish_reason"] = "length"
    else:
        data["parsing_error"] = ValueError("bad format")
    provider = FakeProvider(data)
    with pytest.raises(ModelCallError) as caught:
        await LangChainModelClient(chat_model=provider).generate(REQUEST)
    assert caught.value.code == expected


async def test_cancellation_is_not_normalized():
    provider = FakeProvider(exception=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await LangChainModelClient(chat_model=provider).generate(REQUEST)


def test_explicit_mode_and_provider_retry_contract():
    provider = FakeProvider()
    with pytest.raises(ValueError):
        LangChainModelClient(chat_model=provider, mode="json_mode")
    provider.max_retries = 2
    with pytest.raises(ValueError):
        LangChainModelClient(chat_model=provider)


@pytest.mark.parametrize(
    "schema,payload",
    [
        ({"type": "string"}, "text"),
        ({"type": "null"}, None),
        ({"type": "array", "items": {"type": "integer"}}, [1, 2]),
    ],
)
async def test_non_object_schema_transport_is_lossless(schema, payload):
    from dataclasses import replace

    provider = FakeProvider(response(parsed={"value": payload}))
    result = await LangChainModelClient(chat_model=provider).generate(
        replace(REQUEST, output_schema=schema)
    )
    assert result.payload == payload
    assert provider.schema["properties"]["value"] == schema
    assert result.response_metadata["wrapped_root"] is True


async def test_duplicate_json_keys_are_not_accepted_after_provider_parse():
    data = response(parsed={"x": 2})
    data["raw"].additional_kwargs["tool_calls"] = [{"function": {"arguments": '{"x":1,"x":2}'}}]
    provider = FakeProvider(data)
    with pytest.raises(ModelCallError) as caught:
        await LangChainModelClient(chat_model=provider).generate(REQUEST)
    assert caught.value.code == "MODEL_RESPONSE_INVALID"


@pytest.mark.parametrize("parsed", [None, {"partial": "not a complete response"}])
async def test_invalid_normalized_tool_arguments_are_rejected_and_available_for_repair(parsed):
    data = response(parsed=parsed)
    data["raw"].content = ""
    data["raw"].invalid_tool_calls = [
        {"name": "StructuredResponse", "args": '{"answer": broken}', "error": "parser diagnostic"}
    ]
    provider = FakeProvider(data)
    with pytest.raises(ModelCallError) as caught:
        await LangChainModelClient(chat_model=provider).generate(REQUEST)
    assert caught.value.code == "MODEL_RESPONSE_INVALID" and caught.value.retryable
    assert caught.value.raw_response == '{"answer": broken}'
    assert caught.value.details["json_syntax"]["position"] == 11
    assert caught.value.details["json_syntax"]["line"] == 1
    assert "parser diagnostic" not in str(caught.value)


@pytest.mark.parametrize("mode", ["json_mode", "function_calling"])
@pytest.mark.parametrize("enforce_budget", [True, False])
async def test_real_structured_pipeline_binds_each_request_output_limit(monkeypatch, mode, enforce_budget):
    import json
    from dataclasses import replace

    import httpx
    from langchain_openai import ChatOpenAI

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    bodies = []

    async def send(request):
        bodies.append(json.loads(request.content))
        message = {"role": "assistant", "content": "{}"}
        if mode == "function_calling":
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "test-call", "type": "function", "function": {"name": "StructuredResponse", "arguments": "{}"}}
            ]}
        return httpx.Response(200, request=request, json={
            "id": "test", "object": "chat.completion", "created": 1, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http_client:
        provider = ChatOpenAI(model="test-model", max_retries=0, http_async_client=http_client)
        client = LangChainModelClient(chat_model=provider, mode=mode, allow_json_mode=mode == "json_mode", enforce_output_budget=enforce_budget)
        await asyncio.gather(*(
            client.generate(replace(REQUEST, max_output_tokens=limit)) for limit in (17, 29)
        ))
    limits = sorted(body.get("max_completion_tokens", body.get("max_tokens", 0)) for body in bodies)
    assert limits == ([17, 29] if enforce_budget else [0, 0])
    assert client.metadata["output_budget_enforced"] is enforce_budget
    assert provider.max_tokens is None  # No mutation of a shared provider between concurrent calls.
