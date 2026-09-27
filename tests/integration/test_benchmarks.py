import pytest

from dynamic_graph.devtools.fixtures import cases


@pytest.mark.parametrize("case", cases(), ids=lambda c: c.name)
async def test_reference_task_and_caller_acceptance(case, tmp_path):
    engine = case.engine(tmp_path)
    result = await engine._run(goal=case.goal, policy=case.policy, graph_spec=case.graph)
    assert case.accepts(result), result.model_dump()
    assert result.usage["model_calls"] == 0
    assert all(r["commit_state"] == "committed" for r in result.node_records)


@pytest.mark.parametrize("case", cases(), ids=lambda c: c.name)
async def test_reference_task_failure_injection(case, tmp_path):
    engine = case.engine(tmp_path, failure=True)
    result = await engine._run(goal=case.goal, policy=case.policy, graph_spec=case.graph)
    assert result.execution_status == "FAILED" and not case.accepts(result)
    assert any(d.code == "TOOL_FAILED" for d in result.diagnostics)
