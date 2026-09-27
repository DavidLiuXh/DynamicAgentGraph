"""Deterministic binding only; compilation cannot invoke a model or tool."""

from dataclasses import dataclass

from ..execution.errors import RunFailure
from .nodes import build_node
from .schemas import validate_value
from .state import build_state, unwrap
from .validation import binding_value


@dataclass
class CompiledGraph:
    graph: object
    summary: dict


def compile_graph(spec, goal, snapshot, report, runtime=None):
    from langgraph.graph import END, START, StateGraph

    if not report.valid:
        raise RunFailure(
            "COMPILATION_FAILED", "Cannot compile an invalid graph", phase="compilation"
        )
    builder = StateGraph(build_state(report))
    nodes = {n.id: n for n in spec.nodes}
    for name in report.order:
        builder.add_node(name, build_node(nodes[name], runtime))
    for name in report.order:
        predecessors = sorted(nodes[name].depends_on)
        builder.add_edge(
            predecessors if len(predecessors) > 1 else (predecessors[0] if predecessors else START),
            name,
        )

    async def finalize(state):
        values = unwrap(state)
        outputs = {
            name: binding_value(binding, goal.inputs, values)
            for name, binding in spec.outputs.items()
        }
        try:
            validate_value(goal.output_schema, outputs)
        except ValueError as exc:
            raise RunFailure(
                "OUTPUT_CONTRACT_MISMATCH", "Final output violates contract", phase="output"
            ) from exc
        return {}

    builder.add_node("__finalize__", finalize)
    nonleaves = {p for n in spec.nodes for p in n.depends_on}
    leaves = sorted(set(nodes) - nonleaves)
    builder.add_edge(leaves if len(leaves) > 1 else leaves[0], "__finalize__")
    builder.add_edge("__finalize__", END)
    summary = {
        "nodes": [
            {
                "id": name,
                "depends_on": sorted(nodes[name].depends_on),
                "kind": nodes[name].kind,
                "capability": nodes[name].capability.key if nodes[name].capability else None,
                "input_bindings": {
                    k: v.model_dump(mode="json", exclude_none=True)
                    for k, v in nodes[name].input_bindings.items()
                },
                "writes": [w.model_dump() for w in nodes[name].writes],
                "instruction": nodes[name].instruction,
            }
            for name in report.order
        ],
        "state": {
            k: v.model_dump(mode="json", by_alias=True, exclude_none=True)
            for k, v in sorted(spec.state_fields.items())
        },
        "finalize_predecessors": leaves,
        "snapshot_hash": snapshot.snapshot_hash,
    }
    return CompiledGraph(builder.compile(), summary)
