import asyncio
import json
import os
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from dynamic_graph import (
    DynamicGraphEngine,
    EngineConfig,
    ExecutionPolicy,
    FakeModelClient,
    GoalSpec,
    ModelBindings,
)
from dynamic_graph.models.adapters import LangChainModelClient
from dynamic_graph.tools import tavily_search_tool, web_fetch_tool

QUERIES = ("places", "species", "access")


def object_schema(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def search_pipeline(tmp_path, *, shared_field=False, result_counts=(2, 2, 2)):
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
    for query, count in zip(QUERIES, result_counts, strict=True):
        rows[query] = rows[query][:count]
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


@pytest.mark.parametrize("result_counts", [(0, 0, 0), (1, 1, 1), (2, 2, 2), (0, 1, 2)])
@pytest.mark.parametrize("order", [QUERIES, tuple(reversed(QUERIES))])
async def test_separate_search_fields_preserve_all_results_and_wait_for_every_branch(
    tmp_path, result_counts, order
):
    engine, goal, policy, worker, rows, started, release, returned = search_pipeline(
        tmp_path, result_counts=result_counts
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


def fixed_fetch_candidate(graph):
    """Reproduce unsafe indices, object-to-array writes, and parallel replace writers."""
    candidate = deepcopy(graph)
    fetch = web_fetch_tool()
    pages = {"type": "array", "items": fetch.output_schema}
    candidate["state_fields"]["pages"] = {
        "value_schema": pages,
        "update_schema": pages,
        "initial": {"literal": []},
        "reducer": {"name": "builtin.replace", "version": "1.0.0", "config": {}},
    }
    for query in QUERIES[:2]:
        candidate["nodes"].append(
            {
                "id": f"fetch_{query}",
                "kind": "tool",
                "capability": {"name": fetch.name, "version": fetch.version},
                "input_schema": fetch.input_schema,
                "output_schema": fetch.output_schema,
                "input_bindings": {
                    "url": {"source": "state", "field": f"{query}_results", "pointer": "/0/url"}
                },
                "depends_on": [f"search_{query}"],
                "writes": [{"field": "pages", "output_pointer": ""}],
            }
        )
    return candidate


def node_colliding_candidate(graph):
    candidate = deepcopy(graph)
    old_field, colliding_field = "places_results", "search_places"
    candidate["state_fields"][colliding_field] = candidate["state_fields"].pop(old_field)
    for node in candidate["nodes"]:
        for write in node["writes"]:
            if write["field"] == old_field:
                write["field"] = colliding_field
        for binding in node["input_bindings"].values():
            if binding.get("field") == old_field:
                binding["field"] = colliding_field
    return candidate


@pytest.mark.parametrize("failure_kind", ["fixed_fetch", "node_collision", "both"])
@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("result_counts", [(0, 0, 0), (1, 1, 1), (0, 1, 2)])
async def test_structural_errors_reach_repair_before_any_tool_runs(
    tmp_path, language, result_counts, failure_kind
):
    engine, goal, policy, worker, rows, started, release, returned = search_pipeline(
        tmp_path, result_counts=result_counts
    )
    good = next(engine.models.planner.responses)
    bad_graph = good["graph"]
    if failure_kind in {"fixed_fetch", "both"}:
        bad_graph = fixed_fetch_candidate(bad_graph)
    if failure_kind in {"node_collision", "both"}:
        bad_graph = node_colliding_candidate(bad_graph)
    bad = {**good, "graph": bad_graph}

    class RepairPlanner(FakeModelClient):
        async def generate(self, request):
            if self.requests:
                assert not started.is_set() and not worker.requests
                assert not any(event.is_set() for event in returned.values())
                assert request.input_data["goal"] == goal.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
                repair = request.input_data["repair"]
                assert repair["previous_response"] == bad
                codes = [error["code"] for error in repair["validation_errors"]]
                has_fetch = failure_kind in {"fixed_fetch", "both"}
                assert codes.count("INVALID_BINDING") == (2 if has_fetch else 0)
                assert codes.count("TYPE_MISMATCH") == (2 if has_fetch else 0)
                assert codes.count("UNSAFE_PARALLEL_REDUCER") == (1 if has_fetch else 0)
                assert codes.count("INVALID_STATE_FIELD") == (
                    1 if failure_kind in {"node_collision", "both"} else 0
                )
            return await super().generate(request)

    planner = RepairPlanner([bad, good])
    engine.models = ModelBindings(planner, worker)

    async def unexpected_fetch(data, context):
        raise AssertionError("Full-page fetching is not required by this synthetic goal")

    engine.register_tool(replace(web_fetch_tool(), handler=unexpected_fetch))
    policy.allowed_tools.append("web.fetch@1.0.0")
    policy.max_planning_rounds = 2
    if language == "zh":
        goal.objective = "分别搜索三个主题，汇总每个主题的全部结果，允许结果为空。"
    for event in release.values():
        event.set()
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "COMPLETED", result.diagnostics
    assert result.outputs == {"groups": rows}
    assert len(planner.requests) == 2 and result.usage["tool_calls"] == 3
    assert {record["node_id"] for record in result.node_records} == {
        "search_places",
        "search_species",
        "search_access",
        "compose",
    }
    manifest = json.loads((Path(result.recording["path"]) / "manifest.json").read_text())
    assert manifest["prompt_version"] == "1.1" and manifest["prompt_language"] == language


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("DYNAMIC_GRAPH_LIVE_TESTS") != "1" or not os.environ.get("DEEPSEEK_API_KEY"),
    reason="Opt-in paid DeepSeek test with entirely synthetic inputs and local search handlers",
)
@pytest.mark.parametrize(
    "language, result_counts, repair",
    [
        ("zh", (0, 0, 0), False),
        ("en", (0, 0, 0), False),
        ("zh", (1, 1, 1), False),
        ("en", (1, 1, 1), False),
        ("zh", (0, 1, 2), False),
        ("en", (0, 1, 2), False),
        ("zh", (0, 1, 2), True),
        ("en", (0, 1, 2), True),
    ],
)
async def test_live_planner_handles_variable_search_collections(
    tmp_path, language, result_counts, repair
):
    engine, goal, policy, _, rows, _, release, _ = search_pipeline(
        tmp_path, result_counts=result_counts
    )
    candidate = next(engine.models.planner.responses)
    bad = {
        **candidate,
        "graph": node_colliding_candidate(fixed_fetch_candidate(candidate["graph"])),
    }
    client = LangChainModelClient(model="deepseek-chat")

    class LivePlanner:
        async def generate(self, request):
            if repair and "repair" not in request.input_data:
                # Only the invalid first candidate is synthetic; its repair is generated by DeepSeek.
                return await FakeModelClient([bad]).generate(request)
            return await client.generate(request)

    engine.models = ModelBindings(LivePlanner(), client)
    goal.objective = (
        "分别用搜索工具查询输入中的 places、species、access 三个主题，"
        "按原主题分组返回全部搜索条目，完整保留各条目的字段。结果可以为空。"
        "只需搜索返回的资料，无需网页全文或文件。所有输入与工具结果均为合成测试数据。"
        if language == "zh"
        else "Search each of the three input queries places, species, and access. Return every "
        "search item unchanged in its original query group, preserving all item fields. Results "
        "may be empty. Only search evidence is required, without full pages or files. All input "
        "and tool results are synthetic test data."
    )

    async def unexpected_fetch(data, context):
        raise AssertionError("Full-page fetching is not required by this synthetic goal")

    engine.register_tool(replace(web_fetch_tool(), handler=unexpected_fetch))
    policy.allowed_tools.append("web.fetch@1.0.0")
    policy.max_planning_rounds = 3
    for event in release.values():
        event.set()
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "COMPLETED", result.diagnostics
    assert result.output_complete and result.outputs == {"groups": rows}
    assert result.usage["tool_calls"] == 3
    assert not any(record["node_id"].startswith("fetch_") for record in result.node_records)
