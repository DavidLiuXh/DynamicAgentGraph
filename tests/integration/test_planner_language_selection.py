import json
from pathlib import Path

import pytest

from dynamic_graph.planning.prompts import resources


@pytest.mark.parametrize(
    "objective, language",
    [
        ("收集两项发现并生成摘要", "zh"),
        ("Collect two findings and a summary", "en"),
        ("Collect 两项发现 and a summary", "zh"),
    ],
)
async def test_initial_and_repair_prompts_follow_goal_and_are_recorded(
    setup_run, reference, objective, language
):
    engine, goal, policy, model = setup_run(
        responses=[
            {},
            {"response_version": "1.0", "outcome": "graph", "graph": reference, "diagnostics": []},
            {"report": "summary"},
        ]
    )
    goal.objective = objective
    # Chinese business input is data, not a signal overriding an English objective.
    goal.inputs["query_a"] = "中文检索材料"
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "COMPLETED"
    system, repair, _, versions = resources(language)
    initial, correction = [request for request in model.requests if request.role == "planner"]
    assert initial.system_instruction == correction.system_instruction == system
    assert initial.task_instruction == (
        "生成 PlanningResponse。" if language == "zh" else "Generate PlanningResponse."
    )
    assert correction.task_instruction == repair
    assert correction.input_data["goal"]["objective"] == objective
    assert correction.input_data["repair"]["validation_errors"]
    run_dir = Path(result.recording["path"])
    manifest = json.loads((run_dir / "manifest.json").read_text())
    for key, value in versions.items():
        assert manifest[key] == value
    for attempt in (1, 2):
        recorded = json.loads((run_dir / f"planning/request-{attempt:02d}.json").read_text())
        assert recorded["prompt_language"] == language
        assert recorded["prompt_hash"] == versions["prompt_hash"]
        assert recorded["repair_hash"] == versions["repair_hash"]
