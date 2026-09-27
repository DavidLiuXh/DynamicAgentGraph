import copy

import pytest

from dynamic_graph.graph.compiler import compile_graph
from dynamic_graph.graph.spec import GraphSpec
from dynamic_graph.graph.validation import validate_graph


def damage(graph, case):
    a, b, c = graph["nodes"]
    field = graph["state_fields"]["findings"]
    if case == "cycle":
        a["depends_on"] = ["compose"]
    elif case == "self_cycle":
        a["depends_on"] = ["search_a"]
    elif case == "missing_predecessor":
        a["depends_on"] = ["absent"]
    elif case == "duplicate_predecessor":
        c["depends_on"].append("search_a")
    elif case == "duplicate_id":
        b["id"] = "search_a"
    elif case == "missing_aggregate_dependency":
        c["depends_on"] = ["search_a"]
    elif case == "no_dependencies":
        c["depends_on"] = []
    elif case == "unknown_state_read":
        c["input_bindings"]["findings"]["field"] = "missing"
    elif case == "unknown_state_write":
        a["writes"][0]["field"] = "missing"
    elif case == "multiple_replace":
        field["reducer"] = {"name": "builtin.replace", "version": "1.0.0", "config": {}}
    elif case == "serial_aggregate":
        b["depends_on"] = ["search_a"]
    elif case == "read_and_write":
        c["writes"].append({"field": "findings", "output_pointer": "/report"})
    elif case == "changed_tool_contract":
        a["input_schema"]["properties"]["query"]["type"] = "integer"
    elif case == "missing_output":
        del graph["outputs"]["report"]
    elif case == "extra_output":
        graph["outputs"]["extra"] = graph["outputs"]["report"]
    elif case == "output_from_input":
        graph["outputs"]["report"] = {"source": "input", "pointer": "/query_a"}
    elif case == "output_type":
        graph["outputs"]["report"]["field"] = "findings"
    elif case == "unknown_capability":
        a["capability"]["name"] = "unknown.tool"
    elif case == "wrong_version":
        a["capability"]["version"] = "9.0.0"
    elif case == "bad_pointer":
        a["input_bindings"]["query"]["pointer"] = "/~2"
    elif case == "missing_input":
        a["input_bindings"]["query"]["pointer"] = "/absent"
    elif case == "optional_property_read":
        a["output_schema"]["required"] = []
    elif case == "bad_write_pointer":
        a["writes"][0]["output_pointer"] = "/absent"
    elif case == "duplicate_write":
        a["writes"].append(copy.deepcopy(a["writes"][0]))
    elif case == "invalid_reducer_config":
        field["reducer"]["config"]["on_conflict"] = "replace"
    elif case == "uninitialized_evidence":
        field["initial"]["literal"] = [{"id": "a", "text": "fake"}]
    elif case == "unsafe_import":
        a["capability"]["name"] = "os.system:eval"
    elif case == "unknown_keyword":
        field["value_schema"]["pattern"] = "arbitrary"
    elif case == "node_field_collision":
        graph["state_fields"]["search_a"] = graph["state_fields"]["report"]
    elif case == "unproduced_literal_output":
        c["writes"] = []
    elif case == "missing_binding":
        a["input_bindings"] = {}
    elif case == "invalid_literal":
        a["input_bindings"]["query"] = {"literal": 123}
    elif case == "future_dsl":
        graph["dsl_version"] = "2.0"
    elif case == "empty_graph":
        graph["nodes"] = []
    return graph


CASES = [
    "cycle",
    "self_cycle",
    "missing_predecessor",
    "duplicate_predecessor",
    "duplicate_id",
    "missing_aggregate_dependency",
    "no_dependencies",
    "unknown_state_read",
    "unknown_state_write",
    "multiple_replace",
    "serial_aggregate",
    "read_and_write",
    "changed_tool_contract",
    "missing_output",
    "extra_output",
    "output_from_input",
    "output_type",
    "unknown_capability",
    "wrong_version",
    "bad_pointer",
    "missing_input",
    "optional_property_read",
    "bad_write_pointer",
    "duplicate_write",
    "invalid_reducer_config",
    "uninitialized_evidence",
    "unsafe_import",
    "unknown_keyword",
    "node_field_collision",
    "unproduced_literal_output",
    "missing_binding",
    "invalid_literal",
    "future_dsl",
    "empty_graph",
]


@pytest.mark.parametrize("case", CASES)
def test_invalid_graphs_rejected_before_any_call(case, reference, setup_run):
    engine, goal, policy, model = setup_run([])
    damaged = damage(reference, case)
    try:
        spec = GraphSpec.model_validate(damaged)
    except ValueError:
        return
    report = validate_graph(spec, goal, engine._registry.snapshot(policy), policy)
    assert not report.valid, case
    assert not model.requests


def test_compiler_is_deterministic_and_has_no_model_calls(reference, setup_run):
    engine, goal, policy, model = setup_run([])
    spec = GraphSpec.model_validate(reference)
    snapshot = engine._registry.snapshot(policy)
    report = validate_graph(spec, goal, snapshot, policy)
    summaries = [compile_graph(spec, goal, snapshot, report).summary for _ in range(10)]
    assert all(s == summaries[0] for s in summaries)
    assert not model.requests


@pytest.mark.parametrize(
    "limit,value", [("max_nodes", 2), ("max_state_fields", 1), ("max_graph_bytes", 100)]
)
def test_graph_limits(reference, setup_run, limit, value):
    engine, goal, policy, _ = setup_run([])
    setattr(policy, limit, value)
    report = validate_graph(
        GraphSpec.model_validate(reference), goal, engine._registry.snapshot(policy), policy
    )
    assert not report.valid
