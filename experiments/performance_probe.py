"""Record the 32-node/64-field validation and compilation baseline without a model."""

import asyncio
import json
import platform
import statistics
import time
import tracemalloc
from pathlib import Path
from tempfile import TemporaryDirectory

from dynamic_graph import (
    DynamicGraphEngine,
    EngineConfig,
    ExecutionPolicy,
    FakeModelClient,
    GoalSpec,
    ModelBindings,
    ToolDefinition,
)
from dynamic_graph.devtools.fixtures import INTEGER, obj
from dynamic_graph.graph.compiler import compile_graph
from dynamic_graph.graph.spec import GraphSpec
from dynamic_graph.graph.validation import validate_graph


async def main():
    source = obj({"n": INTEGER})
    output = obj({"value": INTEGER, "double": INTEGER})

    async def calculate(data, context):
        return {"value": data["n"], "double": data["n"] * 2}

    with TemporaryDirectory() as tmp:
        model = FakeModelClient([])
        engine = DynamicGraphEngine(
            config=EngineConfig(runs_dir=tmp), models=ModelBindings(model, model)
        )
        engine.register_tool(
            ToolDefinition(
                "fixture.number", "1.0.0", "Calculate two integers", source, output, calculate
            )
        )
        fields, nodes, outputs, expected = {}, [], {}, {}
        for index in range(32):
            writes = []
            for key, multiplier in [("value", 1), ("double", 2)]:
                name = f"{key}_{index}"
                fields[name] = {
                    "value_schema": INTEGER,
                    "update_schema": INTEGER,
                    "initial": {"literal": 0},
                    "reducer": {"name": "builtin.replace", "version": "1.0.0", "config": {}},
                }
                outputs[name] = {"source": "state", "field": name, "pointer": ""}
                writes.append({"field": name, "output_pointer": "/" + key})
                expected[name] = index * multiplier
            nodes.append(
                {
                    "id": f"n{index}",
                    "kind": "tool",
                    "capability": {"name": "fixture.number", "version": "1.0.0"},
                    "input_schema": source,
                    "input_bindings": {"n": {"literal": index}},
                    "output_schema": output,
                    "writes": writes,
                    "depends_on": [],
                }
            )
        graph = GraphSpec.model_validate(
            {"dsl_version": "1.0", "state_fields": fields, "nodes": nodes, "outputs": outputs}
        )
        goal = GoalSpec(
            objective="Calculate the fixed integer pairs",
            output_schema=obj({k: INTEGER for k in outputs}),
        )
        policy = ExecutionPolicy(allowed_tools=["fixture.number@1.0.0"])
        snapshot = engine._registry.snapshot(policy)
        elapsed = []
        tracemalloc.start()
        for _ in range(10):
            start = time.perf_counter()
            report = validate_graph(graph, goal, snapshot, policy)
            assert report.valid
            compile_graph(graph, goal, snapshot, report)
            elapsed.append(time.perf_counter() - start)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        result = await engine._run(goal=goal, policy=policy, graph_spec=graph)
        assert result.execution_status == "COMPLETED" and result.outputs == expected
        assert not model.requests and result.usage["tool_calls"] == 32
        report = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "nodes": 32,
            "state_fields": 64,
            "iterations": 10,
            "tracemalloc_enabled": True,
            "validation_compile_p50_seconds": statistics.median(elapsed),
            "validation_compile_p95_seconds": sorted(elapsed)[-1],
            "peak_traced_bytes": peak,
            "offline_execution": "passed",
            "model_calls": 0,
            "tool_calls": 32,
        }
        Path("experiments/performance.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))


if __name__ == "__main__":
    asyncio.run(main())
