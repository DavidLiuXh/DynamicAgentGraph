import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from dynamic_graph import (
    DynamicGraphEngine,
    EngineConfig,
    ExecutionPolicy,
    FakeModelClient,
    GoalSpec,
    ModelBindings,
)
from dynamic_graph.tools import tavily_search_tool

QUERIES = ("places", "species", "access")


def object_schema(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def search_pipeline(tmp_path, *, shared_field=False, empty_last=False):
    tool = tavily_search_tool(api_key="synthetic-test-key")
    rows_schema = tool.output_schema["properties"]["results"]
    # A URL identifies a page, not its query-specific score or excerpt.
    rows = {
        query: [
            {
                "title": "Shared page",
                "url": "https://example.com/shared",
                "content": f"Excerpt for {query}",
                "score": 0.9 - index / 10,
            },
            {
                "title": f"Page for {query}",
                "url": f"https://example.com/{query}",
                "content": f"Unique information about {query}",
                "score": 0.5,
            },
        ]
        for index, query in enumerate(QUERIES)
    }
    if empty_last:
        rows["access"] = []
    started = set()
    all_started = asyncio.Event()
    release = {query: asyncio.Event() for query in QUERIES}
    returned = {query: asyncio.Event() for query in QUERIES}

    async def search(data, context):
        query = data["query"]
        started.add(query)
        if len(started) == len(QUERIES):
            all_started.set()
        await release[query].wait()
        returned[query].set()
        return {"query": query, "results": deepcopy(rows[query])}

    fields, nodes = {}, []
    for query in QUERIES:
        field = "search_results" if shared_field else f"{query}_results"
        fields[field] = {
            "value_schema": rows_schema,
            "update_schema": rows_schema,
            "initial": {"literal": []},
            "reducer": {
                "name": "builtin.merge_by_key" if shared_field else "builtin.replace",
                "version": "1.0.0",
                "config": {"key": "url", "on_conflict": "error"} if shared_field else {},
            },
        }
        nodes.append(
            {
                "id": f"search_{query}",
                "kind": "tool",
                "capability": {"name": tool.name, "version": tool.version},
                "input_schema": tool.input_schema,
                "output_schema": tool.output_schema,
                "input_bindings": {"query": {"source": "input", "pointer": f"/{query}"}},
                "depends_on": [],
                "writes": [{"field": field, "output_pointer": "/results"}],
            }
        )
    bindings = {field: {"source": "state", "field": field, "pointer": ""} for field in fields}
    groups_schema = object_schema({query: rows_schema for query in QUERIES})
    output_schema = object_schema({"groups": groups_schema})
    fields["groups"] = {
        "value_schema": groups_schema,
        "update_schema": groups_schema,
        "initial": {"literal": {query: [] for query in QUERIES}},
        "reducer": {"name": "builtin.replace", "version": "1.0.0", "config": {}},
    }
    nodes.append(
        {
            "id": "compose",
            "kind": "llm",
            "model_role": "worker",
            "instruction": "Preserve the complete results from each query in its named group.",
            "input_schema": object_schema({field: rows_schema for field in bindings}),
            "output_schema": output_schema,
            "input_bindings": bindings,
            "depends_on": [f"search_{query}" for query in QUERIES],
            "writes": [{"field": "groups", "output_pointer": "/groups"}],
        }
    )
    graph = {
        "dsl_version": "1.0",
        "state_fields": fields,
        "nodes": nodes,
        "outputs": {"groups": {"source": "state", "field": "groups", "pointer": ""}},
    }
    planner = FakeModelClient(
        [{"response_version": "1.0", "outcome": "graph", "graph": graph, "diagnostics": []}]
    )

    class EchoGroups(FakeModelClient):
        async def generate(self, request):
            assert all(event.is_set() for event in returned.values())
            assert request.input_data == {f"{query}_results": rows[query] for query in QUERIES}
            return await super().generate(request)

    worker = EchoGroups([{"groups": rows}])
    engine = DynamicGraphEngine(
        config=EngineConfig(runs_dir=tmp_path), models=ModelBindings(planner, worker)
    )
    engine.register_tool(replace(tool, handler=search))
    goal = GoalSpec(
        objective="Search three independent queries and preserve every result for summarization.",
        inputs={query: query for query in QUERIES},
        input_schema=object_schema(
            {query: {"type": "string", "minLength": 1} for query in QUERIES}
        ),
        output_schema=output_schema,
    )
    policy = ExecutionPolicy(allowed_tools=["tavily.search@1.0.0"], max_planning_rounds=1)
    return engine, goal, policy, worker, rows, all_started, release, returned


@pytest.mark.parametrize("empty_last", [False, True])
@pytest.mark.parametrize("order", [QUERIES, tuple(reversed(QUERIES))])
async def test_separate_search_fields_preserve_all_results_and_wait_for_every_branch(
    tmp_path, empty_last, order
):
    engine, goal, policy, worker, rows, started, release, returned = search_pipeline(
        tmp_path, empty_last=empty_last
    )
    task = asyncio.create_task(engine.run(goal=goal, policy=policy))
    try:
        # All three tools must start before any is allowed to return.
        await asyncio.wait_for(started.wait(), 5)
        for query in order[:-1]:
            release[query].set()
            await asyncio.wait_for(returned[query].wait(), 5)
            assert not worker.requests
        release[order[-1]].set()
        result = await asyncio.wait_for(task, 5)
    finally:
        for event in release.values():
            event.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert result.execution_status == "COMPLETED", result.diagnostics
    assert result.output_complete and not result.diagnostics
    assert result.outputs == {"groups": rows}
    assert len(worker.requests) == 1 and result.usage["tool_calls"] == 3
    assert all(record["commit_state"] == "committed" for record in result.node_records)


@pytest.mark.parametrize("difference", ["score", "content", "both"])
async def test_shared_url_key_still_rejects_conflicting_search_observations(tmp_path, difference):
    engine, goal, policy, worker, rows, _, release, _ = search_pipeline(tmp_path, shared_field=True)
    for group in rows.values():
        if difference == "score":
            group[0]["content"] = "Identical excerpt"
        elif difference == "content":
            group[0]["score"] = 0.9
    for event in release.values():
        event.set()
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "FAILED" and not result.output_complete
    failure = next(d for d in result.diagnostics if d.code == "REDUCER_FAILED")
    assert failure.details == {
        "reason": "key_conflict",
        "key_field": "url",
        "field": "search_results",
    }
    assert not worker.requests and result.outputs == {}


async def test_multiple_replace_writers_are_rejected_before_search(tmp_path):
    engine, goal, policy, worker, _, _, _, _ = search_pipeline(tmp_path, shared_field=True)
    planner = engine.models.planner
    response = next(planner.responses)
    response["graph"]["state_fields"]["search_results"]["reducer"] = {
        "name": "builtin.replace",
        "version": "1.0.0",
        "config": {},
    }
    planner.responses = iter([response])
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "FAILED" and result.usage["tool_calls"] == 0
    assert any(
        error["code"] == "UNSAFE_PARALLEL_REDUCER"
        for diagnostic in result.diagnostics
        for error in diagnostic.details.get("validation_errors", [])
    )
    assert not worker.requests and result.outputs == {}
