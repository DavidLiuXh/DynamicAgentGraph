import pytest
from pydantic import ValidationError

from dynamic_graph.contracts import GoalSpec
from dynamic_graph.graph.schemas import (
    compatible,
    infer_schema,
    pointer_schema,
    resolve,
    strict_loads,
    validate_schema,
    validate_value,
)
from dynamic_graph.graph.spec import LiteralBinding
from dynamic_graph.planning.responses import PlanningResponse


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "pattern": "a+"},
        {"type": "object", "additionalProperties": True},
        {"type": "object"},
        {"type": "array"},
        {"type": ["string", "integer"]},
        {"type": ["null", "null"]},
        {"$ref": "https://example.com/schema"},
        {"$ref": "#/$defs/A", "$defs": {"A": {"$ref": "#/$defs/A"}}},
        {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        {"type": "string", "minimum": 1},
        {"type": "array", "items": {}, "minItems": 1},
        {"type": "integer", "enum": [True]},
        {"type": "string", "enum": []},
        {
            "type": "object",
            "properties": {},
            "required": ["missing"],
            "additionalProperties": False,
        },
        {"type": "string", "minLength": 5, "maxLength": 3},
    ],
)
def test_unsupported_or_unsound_schema_is_rejected(schema):
    with pytest.raises(Exception):
        validate_schema(schema)


@pytest.mark.parametrize(
    "source,target,expected",
    [
        ({"type": "integer"}, {"type": "number"}, True),
        ({"type": "number"}, {"type": "integer"}, False),
        ({"type": ["string", "null"]}, {"type": "string"}, False),
        ({"type": "string"}, {"type": ["string", "null"]}, True),
        ({"type": "integer", "minimum": 2}, {"type": "integer", "minimum": 1}, True),
        ({"type": "integer"}, {"type": "integer", "minimum": 1}, False),
        ({"type": "integer", "minimum": 1}, {"type": "integer", "exclusiveMinimum": 1}, False),
        ({"type": "string", "enum": ["a"]}, {"type": "string", "enum": ["a", "b"]}, True),
        ({"type": "string"}, {"type": "string", "enum": ["a"]}, False),
    ],
)
def test_conservative_assignment(source, target, expected):
    assert compatible(source, target) == expected


@pytest.mark.parametrize("value", [[], [1, "x"], [None], {"x": None}])
def test_ambiguous_input_needs_schema(value):
    with pytest.raises(ValueError, match="schema"):
        infer_schema(value)


def test_pointer_escape_and_guaranteed_availability():
    assert resolve({"a/b": {"~": 7}}, "/a~1b/~0") == 7
    for pointer in ["a", "/~2", "/~"]:
        with pytest.raises(ValueError):
            resolve({}, pointer)
    schema = {"type": "array", "items": {"type": "string"}}
    with pytest.raises(ValueError):
        pointer_schema(schema, "/0")
    assert pointer_schema({**schema, "minItems": 1}, "/0") == {"type": "string"}


def test_strict_json_and_null_literal_roundtrip():
    for invalid in ['{"x":1,"x":2}', '{"x":NaN}', "```json\n{}\n```"]:
        with pytest.raises(ValueError):
            strict_loads(invalid)
    assert LiteralBinding(literal=None).model_dump(exclude_none=True) == {"literal": None}
    with pytest.raises(ValueError):
        validate_value({"type": "integer"}, True)


@pytest.mark.parametrize(
    "outcome,graph,diagnostics",
    [
        ("graph", None, []),
        (
            "graph",
            "reference",
            [
                {
                    "reason_code": "CAPABILITY_GAP",
                    "message": "gap",
                    "related_input_paths": [],
                    "missing_information": [],
                    "required_capability_description": [],
                }
            ],
        ),
        ("blocked", None, []),
        (
            "blocked",
            "reference",
            [
                {
                    "reason_code": "CAPABILITY_GAP",
                    "message": "gap",
                    "related_input_paths": [],
                    "missing_information": [],
                    "required_capability_description": [],
                }
            ],
        ),
    ],
)
def test_illegal_planning_envelopes(reference, outcome, graph, diagnostics):
    with pytest.raises(ValidationError):
        PlanningResponse.model_validate(
            {
                "response_version": "1.0",
                "outcome": outcome,
                "graph": reference if graph else None,
                "diagnostics": diagnostics,
            }
        )


def test_local_ref_validates_and_unknown_goal_field_rejected():
    schema = {
        "type": "object",
        "properties": {"x": {"$ref": "#/$defs/N"}},
        "required": ["x"],
        "additionalProperties": False,
        "$defs": {"N": {"type": ["integer", "null"]}},
    }
    validate_schema(schema)
    validate_value(schema, {"x": None})
    assert pointer_schema(schema, "/x") == {"type": ["integer", "null"]}
    with pytest.raises(ValidationError):
        GoalSpec(objective="x", goal_status="MET")


def test_root_reference_does_not_hide_invalid_unused_definition():
    with pytest.raises(ValueError):
        validate_schema(
            {
                "$ref": "#/$defs/Value",
                "$defs": {
                    "Value": {"type": "integer"},
                    "Unused": {"$ref": "https://example.com/remote"},
                },
            }
        )


def test_bindings_isolate_nested_input_state_and_literal_values():
    from dynamic_graph.graph.spec import ReferenceBinding
    from dynamic_graph.graph.state import unwrap
    from dynamic_graph.graph.validation import binding_value

    value = {"nested": ["original"]}
    state = {"items": {"kind": "value", "value": value}}
    inputs = {"items": value}
    literal = LiteralBinding(literal=value)
    for binding in (
        ReferenceBinding(source="input", pointer="/items"),
        ReferenceBinding(source="state", field="items", pointer=""),
        literal,
    ):
        projected = binding_value(binding, inputs, unwrap(state))
        projected["nested"].append("mutation")
    assert inputs["items"] == {"nested": ["original"]}
    assert state["items"]["value"] == {"nested": ["original"]}
    assert literal.literal == {"nested": ["original"]}


def test_equivalent_expands_local_references_and_checks_both_directions():
    from dynamic_graph.graph.schemas import equivalent

    source = {"$ref": "#/$defs/Count", "$defs": {"Count": {"type": "integer", "minimum": 0}}}
    assert equivalent(source, {"type": "integer", "minimum": 0})
    assert not equivalent(source, {"type": "integer"})
    assert not equivalent({"type": "integer"}, source)
    with pytest.raises(ValueError):
        equivalent({"$ref": "#/$defs/Missing"}, source)


def test_expansion_preserves_source_and_isolates_repeated_references():
    from copy import deepcopy

    from dynamic_graph.graph.schemas import expand

    reference = {"$ref": "#/$defs/Labels"}
    source = {
        "type": "object",
        "properties": {"left": reference, "right": reference},
        "required": ["left"],
        "additionalProperties": reference,
        "$defs": {"Labels": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}}},
    }
    original = deepcopy(source)
    expanded = expand(source)
    labels = original["$defs"]["Labels"]
    assert expanded == {
        "type": "object",
        "properties": {"left": labels, "right": labels},
        "required": ["left"],
        "additionalProperties": labels,
    }
    expanded["properties"]["left"]["items"]["enum"].append("c")
    expanded["required"].append("right")
    assert expanded["properties"]["right"] == labels
    assert expanded["additionalProperties"] == labels
    assert source == original
