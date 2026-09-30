import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from dynamic_graph import (
    CancellationToken,
    DynamicGraphEngine,
    EngineConfig,
    ExecutionPolicy,
    FakeModelClient,
    GoalSpec,
    ModelBindings,
)
from dynamic_graph.contracts import CallContext, ConfigurationError
from dynamic_graph.execution.errors import ToolCallError
from dynamic_graph.execution.privacy import contains_sensitive, redact_sensitive
from dynamic_graph.tools import tavily_search_tool

KEY = "tvly-" + "testcredential" * 3
RESULT = {
    "query": "LangGraph documentation",
    "results": [
        {"title": "Graph API", "url": "https://example.com/graph", "content": "Guide", "score": 0.9}
    ],
}


@pytest.fixture
def mock_api(monkeypatch):
    original = httpx.AsyncClient
    requests = []

    def install(handler):
        async def respond(request):
            requests.append(request)
            response = handler(request)
            if hasattr(response, "__await__"):
                response = await response
            return response

        def client(**kwargs):
            return original(transport=httpx.MockTransport(respond), **kwargs)

        monkeypatch.setattr("dynamic_graph.tools.tavily.httpx.AsyncClient", client)
        return requests

    return install


def setup_engine(tmp_path, *, allowed=True, max_results=None):
    tool = tavily_search_tool(api_key=KEY)
    bindings = {"query": {"source": "input", "pointer": "/query"}}
    if max_results is not None:
        bindings["max_results"] = {"literal": max_results}
    graph = {
        "dsl_version": "1.0",
        "state_fields": {
            "search_results": {
                "value_schema": tool.output_schema,
                "update_schema": tool.output_schema,
                "initial": {"literal": {"query": "", "results": []}},
                "reducer": {"name": "builtin.replace", "version": "1.0.0", "config": {}},
            }
        },
        "nodes": [
            {
                "id": "web_search",
                "kind": "tool",
                "capability": {"name": tool.name, "version": tool.version},
                "input_schema": tool.input_schema,
                "output_schema": tool.output_schema,
                "input_bindings": bindings,
                "depends_on": [],
                "writes": [{"field": "search_results", "output_pointer": ""}],
            }
        ],
        "outputs": {
            key: {"source": "state", "field": "search_results", "pointer": "/" + key}
            for key in ("query", "results")
        },
    }
    fake = FakeModelClient(
        [{"response_version": "1.0", "outcome": "graph", "graph": graph, "diagnostics": []}]
    )
    engine = DynamicGraphEngine(
        config=EngineConfig(runs_dir=tmp_path), models=ModelBindings(fake, fake)
    )
    engine.register_tool(tool)
    goal = GoalSpec(
        objective="Search for documentation and preserve source links",
        inputs={"query": RESULT["query"]},
        input_schema=tool.input_schema,
        output_schema=tool.output_schema,
    )
    policy = ExecutionPolicy(
        allowed_tools=["tavily.search@1.0.0"] if allowed else [], max_planning_rounds=1
    )
    return engine, goal, policy, fake


@pytest.mark.parametrize("max_results", [None, 2])
async def test_search_goal_to_graph_to_result_and_key_is_private(tmp_path, mock_api, max_results):
    requests = mock_api(
        lambda _: httpx.Response(
            200, json={**RESULT, "answer": "ignored", "request_id": "request-1"}
        )
    )
    engine, goal, policy, fake = setup_engine(tmp_path, max_results=max_results)
    assert not requests  # Creating/registering/inspecting the tool never calls Tavily.
    assert KEY not in repr(engine.list_capabilities())
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "COMPLETED" and result.outputs == RESULT
    assert result.usage["tool_calls"] == 1 and result.node_records[0]["commit_state"] == "committed"
    assert len(requests) == 1 and str(requests[0].url) == "https://api.tavily.com/search"
    assert requests[0].headers["Authorization"] == "Bearer " + KEY
    payload = json.loads(requests[0].content)
    assert payload["query"] == RESULT["query"] and payload["max_results"] == (max_results or 5)
    assert payload["search_depth"] == "basic" and payload["auto_parameters"] is False
    assert payload["include_answer"] is False and payload["include_raw_content"] is False
    assert KEY not in json.dumps(fake.requests[0].input_data)
    for path in Path(result.recording["path"]).rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text()


@pytest.mark.parametrize(
    "status, attempts",
    [(400, 1), (401, 1), (403, 1), (422, 1), (429, 2), (432, 1), (433, 1), (500, 2), (503, 2)],
)
async def test_http_failures_follow_engine_retry_policy(tmp_path, mock_api, status, attempts):
    requests = mock_api(lambda _: httpx.Response(status, json={"detail": {"error": KEY}}))
    engine, goal, policy, _ = setup_engine(tmp_path)
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "FAILED" and result.diagnostics[-1].code == "TOOL_FAILED"
    assert len(requests) == result.usage["tool_calls"] == attempts
    assert result.outputs == {} and not result.artifacts
    for path in Path(result.recording["path"]).rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text()


async def test_transport_retry_is_owned_by_engine(tmp_path, mock_api):
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        if count == 1:
            raise httpx.ReadTimeout(KEY, request=request)
        return httpx.Response(200, json=RESULT)

    requests = mock_api(respond)
    engine, goal, policy, _ = setup_engine(tmp_path)
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "COMPLETED" and len(requests) == 2
    assert result.node_records[0]["attempts"] == 2
    assert len(result.artifacts) == 1  # Only the successful attempt is submitted.


@pytest.mark.parametrize(
    "response", [httpx.Response(200, text="not json"), httpx.Response(200, json={"query": "q"})]
)
async def test_malformed_response_is_not_retried(tmp_path, mock_api, response):
    requests = mock_api(lambda _: response)
    engine, goal, policy, _ = setup_engine(tmp_path)
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "FAILED" and result.outputs == {} and len(requests) == 1


async def test_response_type_violation_cannot_enter_state(tmp_path, mock_api):
    mock_api(
        lambda _: httpx.Response(
            200, json={"query": "q", "results": [{**RESULT["results"][0], "score": "wrong"}]}
        )
    )
    engine, goal, policy, _ = setup_engine(tmp_path)
    result = await engine.run(goal=goal, policy=policy)
    assert (
        result.diagnostics[-1].code == "NODE_OUTPUT_INVALID"
        and result.outputs == {}
        and not result.artifacts
    )


async def test_empty_search_results_are_valid(tmp_path, mock_api):
    mock_api(lambda _: httpx.Response(200, json={"query": RESULT["query"], "results": []}))
    engine, goal, policy, _ = setup_engine(tmp_path)
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "COMPLETED" and result.outputs["results"] == []


async def test_reflected_tavily_key_cannot_enter_outputs_or_records(tmp_path, mock_api):
    mock_api(
        lambda _: httpx.Response(
            200, json={"query": "q", "results": [{**RESULT["results"][0], "content": KEY}]}
        )
    )
    engine, goal, policy, _ = setup_engine(tmp_path)
    result = await engine.run(goal=goal, policy=policy)
    assert result.diagnostics[-1].code == "NODE_OUTPUT_INVALID" and result.outputs == {}
    assert not result.artifacts
    for path in Path(result.recording["path"]).rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text()


@pytest.mark.parametrize("max_results, allowed", [(21, True), (None, False)])
async def test_invalid_or_unauthorized_graph_never_calls_search(
    tmp_path, mock_api, max_results, allowed
):
    requests = mock_api(lambda _: httpx.Response(200, json=RESULT))
    engine, goal, policy, _ = setup_engine(tmp_path, max_results=max_results, allowed=allowed)
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "FAILED" and not requests and result.usage["tool_calls"] == 0


async def test_cancel_propagates_to_search(tmp_path, mock_api):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def respond(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    requests = mock_api(respond)
    engine, goal, policy, _ = setup_engine(tmp_path)
    token = CancellationToken()
    task = asyncio.create_task(engine.run(goal=goal, policy=policy, cancellation_token=token))
    await asyncio.wait_for(entered.wait(), 5)
    token.cancel()
    result = await asyncio.wait_for(task, 5)
    assert result.execution_status == "CANCELLED" and cancelled.is_set() and len(requests) == 1


async def test_expired_deadline_never_makes_request(mock_api):
    requests = mock_api(lambda _: httpx.Response(200, json=RESULT))
    context = CallContext("run", "node", 1, time.monotonic() - 1, CancellationToken())
    with pytest.raises(TimeoutError):
        await tavily_search_tool(api_key=KEY).handler({"query": "q"}, context)
    assert not requests


async def test_handler_has_no_hidden_retry_and_does_not_expose_provider_body(mock_api):
    requests = mock_api(lambda _: httpx.Response(429, text=KEY))
    context = CallContext("run", "node", 1, time.monotonic() + 10, CancellationToken())
    with pytest.raises(ToolCallError) as exc:
        await tavily_search_tool(api_key=KEY).handler({"query": "q"}, context)
    assert len(requests) == 1 and exc.value.retryable and KEY not in str(exc.value)


def test_key_configuration_and_credential_redaction(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(ConfigurationError):
        tavily_search_tool()
    monkeypatch.setenv("TAVILY_API_KEY", KEY)
    assert tavily_search_tool().name == "tavily.search"
    first = tavily_search_tool()
    first.input_schema["required"].append("max_results")
    first.output_schema["properties"]["results"]["maxItems"] = 1
    second = tavily_search_tool()
    assert second.input_schema["required"] == ["query"]
    assert second.output_schema["properties"]["results"]["maxItems"] == 20
    with pytest.raises(ConfigurationError):
        tavily_search_tool(api_key=" ")
    assert contains_sensitive("provider reflected " + KEY)
    assert redact_sensitive("provider reflected " + KEY) == "provider reflected [REDACTED]"
