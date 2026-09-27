import json
from pathlib import Path

import pytest

from dynamic_graph import (
    DynamicGraphEngine,
    EngineConfig,
    ExecutionPolicy,
    FakeModelClient,
    GoalSpec,
    ModelBindings,
    ToolDefinition,
)
from dynamic_graph.graph.spec import GraphSpec


@pytest.fixture
def reference():
    return json.loads((Path(__file__).parent / "fixtures/reference_graph.json").read_text())


def envelope(graph):
    return {"response_version": "1.0", "outcome": "graph", "graph": graph, "diagnostics": []}


@pytest.fixture
def setup_run(tmp_path, reference):
    def setup(responses=None, handler=None, config=None):
        spec = GraphSpec.model_validate(reference)
        fake = FakeModelClient(
            responses if responses is not None else [envelope(reference), {"report": "summary"}]
        )
        engine = DynamicGraphEngine(
            config=config or EngineConfig(runs_dir=tmp_path), models=ModelBindings(fake, fake)
        )

        async def search(data, context):
            return {"findings": [{"id": context.node_id, "text": data["query"]}]}

        for node in spec.nodes[:2]:
            engine.register_tool(
                ToolDefinition(
                    node.capability.name,
                    "1.0.0",
                    "Synthetic search",
                    node.input_schema.document(),
                    node.output_schema.document(),
                    handler or search,
                )
            )
        goal = GoalSpec(
            objective="Collect two synthetic findings and a summary",
            inputs={"query_a": "A", "query_b": "B"},
            output_schema={
                "type": "object",
                "properties": {
                    "report": {"type": "string"},
                    "findings": spec.state_fields["findings"].value_schema.document(),
                },
                "required": ["report", "findings"],
                "additionalProperties": False,
            },
        )
        policy = ExecutionPolicy(allowed_tools=["demo.search_a@1.0.0", "demo.search_b@1.0.0"])
        return engine, goal, policy, fake

    return setup
