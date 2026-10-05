import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from dynamic_graph import CancellationToken, ModelCallError
from dynamic_graph.execution.errors import RunFailure
from dynamic_graph.graph.spec import GraphSpec
from dynamic_graph.recording.local import Recorder


def envelope(graph):
    return {"response_version": "1.0", "outcome": "graph", "graph": graph, "diagnostics": []}


async def test_goal_to_graph_to_result_and_recording(setup_run):
    engine, goal, policy, model = setup_run()
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "COMPLETED"
    assert result.output_complete
    assert result.outputs == {
        "report": "summary",
        "findings": [{"id": "search_a", "text": "A"}, {"id": "search_b", "text": "B"}],
    }
    assert len(model.requests) == 2
    path = Path(result.recording["path"])
    assert hashlib.sha256((path / "graph.json").read_bytes()).hexdigest() == result.graph_hash
    assert json.loads((path / "result.json").read_text()) == result.model_dump(mode="json")
    events = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert all(r["commit_state"] == "committed" for r in result.node_records)
    assert all(a["commit_state"] == "committed" for a in result.artifacts)


async def test_caller_can_revise_goal_as_new_linked_run(setup_run):
    first_engine, first_goal, policy, _ = setup_run()
    first = await first_engine.run(goal=first_goal, policy=policy)
    engine, goal, _, _ = setup_run()
    goal.parent_run_id = first.run_id
    goal.objective = "Collect additional evidence for the revised request"
    second = await engine.run(goal=goal, policy=policy)
    assert second.execution_status == "COMPLETED" and second.run_id != first.run_id
    saved = json.loads((Path(second.recording["path"]) / "goal.json").read_text())
    assert saved["parent_run_id"] == first.run_id
    assert saved["objective"] != first_goal.objective


async def test_repair_and_network_retry_share_round_budget(setup_run, reference):
    bad = json.loads(json.dumps(reference))
    bad["nodes"][2]["depends_on"] = []
    engine, goal, policy, model = setup_run(
        [
            ModelCallError("MODEL_RATE_LIMITED", "temporary", retryable=True),
            envelope(bad),
            envelope(reference),
            {"report": "fixed"},
        ]
    )
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "COMPLETED"
    assert len([r for r in model.requests if r.role == "planner"]) == 3
    assert model.requests[0].input_data["goal"] == model.requests[2].input_data["goal"]
    assert model.requests[2].input_data["repair"]["validation_errors"]


@pytest.mark.parametrize("recovered", [False, True])
async def test_truncated_worker_regenerates_compact_output_within_attempt_budget(
    setup_run, reference, recovered
):
    error = ModelCallError(
        "MODEL_RESPONSE_TRUNCATED",
        "Provider output cut off",
        retryable=True,
        usage={"input_tokens": 20, "output_tokens": 8},
    )
    engine, goal, policy, model = setup_run(
        [
            envelope(reference),
            error,
            {"report": "compact"} if recovered else error,
        ]
    )
    result = await engine.run(goal=goal, policy=policy)
    calls = [request for request in model.requests if request.role == "worker"]
    assert len(calls) == policy.max_node_attempts == 2
    assert calls[0].input_data == calls[1].input_data
    assert calls[0].output_schema == calls[1].output_schema
    assert calls[1].timeout_seconds <= calls[0].timeout_seconds
    assert "MODEL_RESPONSE_TRUNCATED" in calls[1].task_instruction
    assert "compact JSON" in calls[1].task_instruction
    assert "Preserve all required" in calls[1].task_instruction
    worker = next(node["id"] for node in reference["nodes"] if node["kind"] == "llm")
    worker_artifacts = [artifact for artifact in result.artifacts if artifact["node_id"] == worker]
    if recovered:
        assert result.execution_status == "COMPLETED" and result.output_complete
        assert result.outputs["report"] == "compact"
        assert [artifact["attempt"] for artifact in worker_artifacts] == [2]
    else:
        assert result.execution_status == "FAILED" and not result.output_complete
        assert "report" not in result.outputs and len(result.outputs["findings"]) == 2
        assert not worker_artifacts
        assert result.diagnostics[-1].code == "MODEL_RESPONSE_TRUNCATED"


async def test_blocked_is_terminal_without_graph_or_tools(setup_run):
    blocked = {
        "response_version": "1.0",
        "outcome": "blocked",
        "graph": None,
        "diagnostics": [
            {
                "reason_code": "MISSING_INFORMATION",
                "message": "Need a topic",
                "related_input_paths": [],
                "missing_information": ["topic"],
                "required_capability_description": [],
            }
        ],
    }
    engine, goal, policy, model = setup_run([blocked])
    result = await engine.run(goal=goal, policy=policy)
    assert result.diagnostics[-1].code == "PLANNING_BLOCKED"
    assert len(model.requests) == 1 and result.usage["tool_calls"] == 0
    assert result.graph_ref is None
    assert not (Path(result.recording["path"]) / "graph.json").exists()


async def test_parallel_failure_does_not_publish_uncommitted_data(setup_run, reference):
    returned = asyncio.Event()

    async def handler(data, context):
        if context.node_id == "search_b":
            await returned.wait()
            await asyncio.sleep(0.03)
            raise RuntimeError("injected")
        returned.set()
        return {"findings": [{"id": "a", "text": "available but uncommitted"}]}

    engine, goal, policy, _ = setup_run([envelope(reference)], handler)
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "FAILED" and result.outputs == {}
    assert any(
        a["node_id"] == "search_a" and a["commit_state"] == "uncommitted" for a in result.artifacts
    )


async def test_graph_save_failure_prevents_tool_calls(setup_run, monkeypatch):
    original = Recorder.write

    def broken(self, name, value, **kwargs):
        if name == "graph.json":
            raise RunFailure("RECORDING_FAILED", "Injected disk failure", phase="saving")
        return original(self, name, value, **kwargs)

    monkeypatch.setattr(Recorder, "write", broken)
    engine, goal, policy, _ = setup_run()
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "FAILED" and result.usage["tool_calls"] == 0


@pytest.mark.parametrize("direct", [False, True])
async def test_cancel_propagation_and_no_new_calls(setup_run, reference, direct):
    started = asyncio.Event()
    active = 0

    async def handler(data, context):
        nonlocal active
        active += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    engine, goal, policy, _ = setup_run([envelope(reference)], handler)
    token = CancellationToken()
    task = asyncio.create_task(engine.run(goal=goal, policy=policy, cancellation_token=token))
    await started.wait()
    if direct:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        token.cancel()
        result = await task
        assert result.execution_status == "CANCELLED"
    assert active == 0


async def test_offline_execution_never_calls_planner(setup_run, reference):
    engine, goal, policy, model = setup_run([{"report": "offline"}])
    result = await engine._run(
        goal=goal, policy=policy, graph_spec=GraphSpec.model_validate(reference)
    )
    assert result.execution_status == "COMPLETED"
    assert all(r.role == "worker" for r in model.requests)


async def test_finalization_failure_preserves_output(setup_run, monkeypatch):
    original = Recorder.write

    def broken(self, name, value, **kwargs):
        if name == "result.json":
            raise RunFailure("RECORDING_FAILED", "Injected finalization failure")
        return original(self, name, value, **kwargs)

    monkeypatch.setattr(Recorder, "write", broken)
    engine, goal, policy, _ = setup_run()
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "FAILED" and result.phase == "finalization"
    assert result.output_complete and result.outputs["report"] == "summary"


async def test_model_quota_exhaustion_never_retried(setup_run):
    engine, goal, policy, model = setup_run([ModelCallError("MODEL_QUOTA_EXHAUSTED", "No balance")])
    result = await engine.run(goal=goal, policy=policy)
    assert result.diagnostics[-1].code == "MODEL_QUOTA_EXHAUSTED"
    assert len(model.requests) == 1 and result.usage["tool_calls"] == 0


@pytest.mark.parametrize("limit", ["max_cost", "max_tokens"])
async def test_strict_unenforceable_budget_rejected_before_call(setup_run, limit):
    engine, goal, policy, model = setup_run()
    setattr(policy, limit, 1)
    result = await engine.run(goal=goal, policy=policy)
    assert result.diagnostics[-1].code == "BUDGET_UNENFORCEABLE"
    assert not model.requests


async def test_json_syntax_location_reaches_planning_repair_with_original_response(
    setup_run, reference
):
    raw = '{"response_version": "1.0"}}, "graph": {}}'
    failure = ModelCallError(
        "MODEL_RESPONSE_INVALID",
        "Invalid JSON",
        retryable=True,
        raw_response=raw,
        details={"json_syntax": {"message": "Extra data", "line": 1, "column": 27, "position": 26}},
    )
    engine, goal, policy, model = setup_run([failure, envelope(reference), {"report": "repaired"}])
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "COMPLETED"
    repair = model.requests[1].input_data["repair"]
    assert "Extra data" in repair["validation_errors"][0]["message"]
    assert "27" in repair["validation_errors"][0]["message"]
    assert raw in json.dumps(repair).replace('\\"', '"')
