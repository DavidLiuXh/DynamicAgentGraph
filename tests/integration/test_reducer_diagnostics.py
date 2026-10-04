from copy import deepcopy

import pytest

from dynamic_graph.graph.schemas import SchemaSpec


@pytest.mark.parametrize(
    "reducer, same_text",
    [("builtin.replace", False), ("builtin.merge_by_key", False), ("builtin.merge_by_key", True)],
)
async def test_complete_list_and_keyed_merge_have_distinct_semantics(
    setup_run, reference, reducer, same_text
):
    graph = deepcopy(reference)
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"source": {"type": "string"}, "text": {"type": "string"}},
            "required": ["source", "text"],
            "additionalProperties": False,
        },
    }
    graph["state_fields"]["evidence"] = {
        "value_schema": schema,
        "update_schema": schema,
        "initial": {"literal": []},
        "reducer": {
            "name": reducer,
            "version": "1.0.0",
            "config": {"key": "source", "on_conflict": "error"}
            if reducer == "builtin.merge_by_key"
            else {},
        },
    }
    compose = graph["nodes"][-1]
    compose["output_schema"]["properties"]["evidence"] = schema
    compose["output_schema"]["required"].append("evidence")
    compose["writes"].append({"field": "evidence", "output_pointer": "/evidence"})
    graph["outputs"]["evidence"] = {"source": "state", "field": "evidence", "pointer": ""}
    entries = [
        {"source": "https://example.com/source", "text": "first excerpt"},
        {
            "source": "https://example.com/source",
            "text": "first excerpt" if same_text else "second excerpt",
        },
    ]
    engine, goal, policy, model = setup_run(
        [
            {"response_version": "1.0", "outcome": "graph", "graph": graph, "diagnostics": []},
            {"report": "summary", "evidence": entries},
        ]
    )
    output = goal.output_schema.document()
    output["properties"]["evidence"] = schema
    output["required"].append("evidence")
    goal.output_schema = SchemaSpec.model_validate(output)
    result = await engine.run(goal=goal, policy=policy)
    if reducer == "builtin.merge_by_key" and not same_text:
        assert result.execution_status == "FAILED"
        failure = next(d for d in result.diagnostics if d.code == "REDUCER_FAILED")
        assert failure.node_id == compose["id"]
        assert failure.details == {
            "reason": "key_conflict",
            "key_field": "source",
            "field": "evidence",
        }
        assert "https://example.com/source" not in failure.model_dump_json()
        assert "evidence" not in result.outputs
        assert len([r for r in model.requests if r.role == "worker"]) == 1
    else:
        assert result.execution_status == "COMPLETED"
        assert result.outputs["evidence"] == (
            entries if reducer == "builtin.replace" else entries[:1]
        )
