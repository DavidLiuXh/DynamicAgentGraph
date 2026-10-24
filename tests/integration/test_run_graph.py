import asyncio
import json
from pathlib import Path

import pytest

from dynamic_graph import CancellationToken, GraphSpec


async def test_public_graph_execution_skips_planner_and_isolates_state(setup_run, reference):
    engine, goal, policy, model = setup_run([{'report': 'one'}, {'report': 'two'}])
    spec = GraphSpec.model_validate(reference)
    first = await engine.run_graph(goal=goal, graph_spec=spec, policy=policy)
    second = await engine.run_graph(goal=goal, graph_spec=spec, policy=policy)
    assert first.execution_status == second.execution_status == 'COMPLETED'
    assert first.outputs['report'] == 'one' and second.outputs['report'] == 'two'
    assert first.run_id != second.run_id
    assert len(model.requests) == 2
    assert all(r.role == 'worker' for r in model.requests)
    assert len(first.outputs['findings']) == len(second.outputs['findings']) == 2
    manifest = json.loads((Path(first.recording['path']) / 'manifest.json').read_text())
    assert manifest['compilations'][0]['attempt'] == 0


async def test_public_graph_checks_current_authorization_before_tools(setup_run, reference):
    calls = []

    async def tool(data, context):
        calls.append(data)
        return {'findings': []}

    engine, goal, policy, model = setup_run([], tool)
    policy.allowed_tools = []
    result = await engine.run_graph(goal=goal, graph_spec=GraphSpec.model_validate(reference), policy=policy)
    assert result.execution_status == 'FAILED'
    assert any(d.code == 'GRAPH_VALIDATION_FAILED' for d in result.diagnostics)
    assert not calls and not model.requests


@pytest.mark.parametrize('cancel', [False, True])
async def test_public_graph_preserves_timeout_and_cancel(setup_run, reference, cancel):
    entered = asyncio.Event()

    async def tool(data, context):
        entered.set()
        await asyncio.sleep(5)

    engine, goal, policy, _ = setup_run([], tool)
    policy.run_timeout_seconds = 0.1 if not cancel else 3
    token = CancellationToken()
    task = asyncio.create_task(engine.run_graph(goal=goal, graph_spec=GraphSpec.model_validate(reference), policy=policy, cancellation_token=token))
    if cancel:
        await entered.wait()
        token.cancel()
    result = await task
    assert result.execution_status == ('CANCELLED' if cancel else 'FAILED')
    if not cancel:
        assert any(d.code == 'DEADLINE_EXCEEDED' for d in result.diagnostics)
