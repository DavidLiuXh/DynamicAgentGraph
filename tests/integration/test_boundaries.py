import asyncio
import copy
import json
from pathlib import Path

from dynamic_graph import EngineConfig, ModelCallError
from dynamic_graph.devtools.fixtures import cases
from dynamic_graph.execution.errors import ToolCallError
from dynamic_graph.graph.spec import GraphSpec


def envelope(graph):
    return {"response_version": "1.0", "outcome": "graph", "graph": graph, "diagnostics": []}


async def test_engine_and_run_limit_bound_actual_handlers(setup_run, reference, tmp_path):
    active = peak = 0

    async def handler(data, context):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.03)
            return {"findings": [{"id": context.node_id, "text": data["query"]}]}
        finally:
            active -= 1

    engine, goal, policy, _ = setup_run(
        [{"report": "A"}, {"report": "B"}],
        handler,
        EngineConfig(runs_dir=tmp_path, max_parallelism=2),
    )
    policy.max_parallelism = 1
    results = await asyncio.gather(
        *(
            engine._run(goal=goal, policy=policy, graph_spec=GraphSpec.model_validate(reference))
            for _ in range(2)
        )
    )
    assert peak == 2 and active == 0
    assert all(r.execution_status == "COMPLETED" for r in results)
    assert results[0].run_id != results[1].run_id


async def test_tool_retry_commits_once(setup_run, reference):
    calls = {}

    async def handler(data, context):
        calls[context.node_id] = calls.get(context.node_id, 0) + 1
        if calls[context.node_id] == 1:
            raise ToolCallError(retryable=True)
        return {"findings": [{"id": context.node_id, "text": "ok"}]}

    engine, goal, policy, _ = setup_run([envelope(reference), {"report": "retried"}], handler)
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "COMPLETED" and len(result.outputs["findings"]) == 2
    assert result.usage["tool_calls"] == 4
    events = [
        json.loads(line)
        for line in (Path(result.recording["path"]) / "events.jsonl").read_text().splitlines()
    ]
    for node in ("search_a", "search_b"):
        assert (
            sum(e["event_type"] == "state_committed" and e["node_id"] == node for e in events) == 1
        )


async def test_sensitive_error_never_leaks_to_result_or_records(setup_run, tmp_path):
    secret = "sk-" + "f" * 32
    engine, goal, policy, _ = setup_run([ModelCallError("MODEL_AUTH_FAILED", "Bad key " + secret)])
    result = await engine.run(goal=goal, policy=policy)
    assert secret not in result.model_dump_json()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text()


async def test_conflicting_parallel_reducer_outputs_stay_uncommitted(setup_run, reference):
    async def handler(data, context):
        return {"findings": [{"id": "same", "text": data["query"]}]}

    engine, goal, policy, _ = setup_run([envelope(reference)], handler)
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "FAILED"
    assert result.outputs == {} and not result.output_complete
    assert len(result.artifacts) == 2
    assert all(a["commit_state"] == "uncommitted" for a in result.artifacts)
    assert any(d.code == "REDUCER_FAILED" for d in result.diagnostics)


async def test_worker_schema_failure_retries_without_changing_graph(setup_run, reference):
    engine, goal, policy, model = setup_run(
        [envelope(reference), {"report": 42}, {"report": "fixed"}]
    )
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "COMPLETED"
    assert len(model.requests) == 3
    assert model.requests[1].output_schema == model.requests[2].output_schema
    assert json.loads(Path(result.graph_ref).read_text()) == reference


async def test_timeout_stops_before_dependent_calls(setup_run, reference):
    async def handler(data, context):
        await asyncio.sleep(5)

    engine, goal, policy, model = setup_run([envelope(reference)], handler)
    policy.node_timeout_seconds = 0.03
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "FAILED"
    assert any(d.code == "DEADLINE_EXCEEDED" for d in result.diagnostics)
    assert len(model.requests) == 1


def test_shipped_examples_validate_against_their_own_contracts():
    from dynamic_graph.contracts import GoalSpec
    from dynamic_graph.graph.validation import validate_graph
    from dynamic_graph.planning.prompts import examples
    from dynamic_graph.planning.responses import PlanningResponse

    for example in examples():
        # Shipped examples use an independent namespace; no benchmark reference
        # solution is selected as an in-context answer for its own task.
        example = json.loads(json.dumps(example).replace("example.", "fixture."))
        response = PlanningResponse.model_validate(example["response"])
        if response.outcome == "blocked":
            continue
        case = next((c for c in cases() if c.name == example["case_name"]), cases()[0])
        engine = case.engine("runs")
        report = validate_graph(
            response.graph,
            GoalSpec.model_validate(example["goal"]),
            engine._registry.snapshot(case.policy),
            case.policy,
        )
        assert report.valid


async def test_readonly_declaration_enforced_before_tool_calls(setup_run, reference):
    from dataclasses import replace

    engine, goal, policy, model = setup_run([envelope(reference)] * 3)
    key = "demo.search_a@1.0.0"
    engine._registry._entries[key] = replace(engine._registry._entries[key], read_only=False)
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "FAILED" and result.usage["tool_calls"] == 0
    assert len(model.requests) == 3


async def test_node_multi_field_update_is_atomic(setup_run, reference):
    # The node's first update is valid; its second violates the reducer's U type.
    # No field from that node may appear in committed state or outputs.
    from dynamic_graph import ReducerDefinition
    from dynamic_graph.devtools.fixtures import STRING, obj

    engine, goal, policy, model = setup_run([{"report": "done"}])

    def rejecting(old, update, config):
        raise ValueError("injected reducer failure")

    engine.register_reducer(
        ReducerDefinition(
            "test.reject",
            "1.0.0",
            "Reject business updates",
            STRING,
            STRING,
            obj({}),
            lambda value: value == "",
            rejecting,
        )
    )
    policy.allowed_reducers.append("test.reject@1.0.0")
    graph = copy.deepcopy(reference)
    graph["state_fields"]["second"] = {
        "value_schema": STRING,
        "update_schema": STRING,
        "initial": {"literal": ""},
        "reducer": {"name": "test.reject", "version": "1.0.0", "config": {}},
    }
    graph["nodes"][2]["writes"].append({"field": "second", "output_pointer": "/report"})
    result = await engine._run(goal=goal, policy=policy, graph_spec=GraphSpec.model_validate(graph))
    assert result.execution_status == "FAILED"
    assert "report" not in result.outputs and len(result.outputs["findings"]) == 2
    assert (
        next(r for r in result.node_records if r["node_id"] == "compose")["commit_state"]
        == "uncommitted"
    )


async def test_custom_redactor_does_not_change_planning_or_execution(
    setup_run, reference, tmp_path
):
    marker = "format-only-marker"

    def redact(value):
        if isinstance(value, str):
            return value.replace(marker, "[HIDDEN]")
        if isinstance(value, dict):
            for key in value:
                value[key] = redact(value[key])
        elif isinstance(value, list):
            for index in range(len(value)):
                value[index] = redact(value[index])
        return value

    reference["nodes"][2]["instruction"] += " " + marker
    invalid = copy.deepcopy(reference)
    invalid["nodes"][2]["depends_on"] = []
    engine, goal, policy, model = setup_run(
        [envelope(invalid), envelope(reference), {"report": "done"}],
        config=EngineConfig(runs_dir=tmp_path, redactor=redact),
    )
    goal.objective += " " + marker
    goal.inputs["query_a"] = marker
    result = await engine.run(goal=goal, policy=policy)

    assert result.execution_status == "COMPLETED"
    assert model.requests[0].input_data["goal"]["inputs"]["query_a"] == marker
    previous = model.requests[1].input_data["repair"]["previous_response"]
    assert previous["graph"] == invalid
    assert model.requests[2].input_data["findings"][0]["text"] == marker
    assert json.loads(Path(result.graph_ref).read_text()) == reference
    saved_goal = json.loads((Path(result.recording["path"]) / "goal.json").read_text())
    assert saved_goal["inputs"]["query_a"] == "[HIDDEN]"
    assert result.outputs["findings"][0]["text"] == "[HIDDEN]"
    assert goal.inputs["query_a"] == marker


async def test_worker_mutation_cannot_change_state_or_retry_inputs(setup_run):
    from dynamic_graph import ModelBindings, ModelResponse

    engine, goal, policy, planner = setup_run()

    class Worker:
        def __init__(self):
            self.inputs = []

        async def generate(self, request):
            self.inputs.append(copy.deepcopy(request.input_data))
            request.input_data["findings"][0]["text"] = "mutated by worker"
            return ModelResponse({"report": 42 if len(self.inputs) == 1 else "done"})

    worker = Worker()
    engine.models = ModelBindings(planner=planner, worker=worker)
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "COMPLETED"
    assert len(worker.inputs) == 2 and worker.inputs[0] == worker.inputs[1]
    assert result.outputs["findings"][0]["text"] == "A"
