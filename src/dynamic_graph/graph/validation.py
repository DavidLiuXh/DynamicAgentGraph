"""Deterministic validation. No model calls or business tool invocation."""

import re
from copy import deepcopy
from dataclasses import dataclass, field

from ..capabilities.builtins import bind_reducer
from ..contracts import Diagnostic
from .schemas import (
    SchemaError,
    canonical,
    compatible,
    equivalent,
    expand,
    pointer_schema,
    resolve,
    validate_schema,
    validate_value,
)
from .spec import LiteralBinding


@dataclass
class ValidationReport:
    diagnostics: list = field(default_factory=list)
    order: list = field(default_factory=list)
    ancestors: dict = field(default_factory=dict)
    writers: dict = field(default_factory=dict)
    reducers: dict = field(default_factory=dict)
    initial_values: dict = field(default_factory=dict)

    @property
    def valid(self):
        return not any(d.severity == "error" for d in self.diagnostics)


def binding_value(binding, inputs, state):
    if isinstance(binding, LiteralBinding):
        return deepcopy(binding.literal)
    root = inputs if binding.source == "input" else state[binding.field]
    # Bindings cross into node inputs/public outputs; isolate only the selected value.
    return deepcopy(resolve(root, binding.pointer))


def validate_graph(spec, goal, snapshot, policy):
    report = ValidationReport()
    nodes = {node.id: node for node in spec.nodes}
    report.writers = {name: set() for name in spec.state_fields}

    def error(code, message, path="", node_id=None, severity="error"):
        report.diagnostics.append(
            Diagnostic(
                code=code,
                phase="validation",
                message=message,
                path=path,
                node_id=node_id,
                severity=severity,
            )
        )

    def guard(fn, path, node_id=None):
        try:
            return fn()
        except SchemaError as exc:
            error(exc.code, str(exc), path + exc.path, node_id)
        except (ValueError, KeyError, TypeError):
            error("INVALID_SCHEMA", "Schema or binding violates its contract", path, node_id)
        return None

    if len(canonical(spec.document())) > policy.max_graph_bytes:
        error("PLAN_LIMIT_EXCEEDED", "Graph exceeds byte limit")
    if len(spec.nodes) > policy.max_nodes or len(spec.state_fields) > policy.max_state_fields:
        error("PLAN_LIMIT_EXCEEDED", "Graph exceeds node/field count limit")
    if len(nodes) != len(spec.nodes):
        error("DUPLICATE_NODE", "Node identifiers must be unique", "/nodes")
    for name in spec.state_fields:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) or name in nodes:
            error(
                "INVALID_STATE_FIELD",
                "Invalid, reserved, or node-colliding state name",
                f"/state_fields/{name}",
            )
    for index, node in enumerate(spec.nodes):
        path = f"/nodes/{index}"
        if len(set(node.depends_on)) != len(node.depends_on):
            error("DUPLICATE_DEPENDENCY", "Duplicate predecessor", path + "/depends_on", node.id)
        for dep in node.depends_on:
            if dep not in nodes:
                error(
                    "UNKNOWN_DEPENDENCY",
                    "Predecessor does not exist",
                    path + "/depends_on",
                    node.id,
                )
        writes = [write.field for write in node.writes]
        if len(set(writes)) != len(writes):
            error(
                "DUPLICATE_WRITE", "A node can update a field only once", path + "/writes", node.id
            )
        for name in writes:
            if name not in report.writers:
                error(
                    "UNKNOWN_STATE_FIELD", "Write field does not exist", path + "/writes", node.id
                )
            else:
                report.writers[name].add(node.id)
    pending = set(nodes)
    while pending:
        ready = sorted(n for n in pending if set(nodes[n].depends_on).issubset(report.ancestors))
        if not ready:
            error("GRAPH_CYCLE", "Graph contains a cycle or invalid dependency", "/nodes")
            break
        for name in ready:
            predecessors = set(nodes[name].depends_on)
            report.ancestors[name] = predecessors | set().union(
                *(report.ancestors[p] for p in predecessors)
            )
            report.order.append(name)
            pending.remove(name)
    if pending or len(nodes) != len(spec.nodes):
        return report

    for name, definition in spec.state_fields.items():
        path = f"/state_fields/{name}"
        for key in ("value_schema", "update_schema"):
            guard(
                lambda: validate_schema(getattr(definition, key), policy.max_schema_depth),
                path + "/" + key,
            )
        reducer = guard(lambda: bind_reducer(definition, snapshot, policy), path + "/reducer")
        if reducer:
            report.reducers[name] = reducer
            initial = guard(
                lambda: reducer.initial(binding_value(definition.initial, goal.inputs, {})),
                path + "/initial",
            )
            # None may itself be a valid value; valid is decided from diagnostics.
            report.initial_values[name] = initial
        writers = report.writers[name]
        if len(writers) > 1:
            if reducer and not reducer.parallel_safe:
                error(
                    "UNSAFE_PARALLEL_REDUCER",
                    "Multiple writers require a parallel-safe reducer",
                    path,
                )
            if any(a in report.ancestors[b] for a in writers for b in writers if a != b):
                error("SERIAL_AGGREGATE_WRITE", "Aggregate writers must be independent", path)

    def source_schema(binding, reader=None):
        if binding.source == "input":
            # Require both actual presence and a schema-guaranteed pointer path.
            resolve(goal.inputs, binding.pointer)
            return pointer_schema(goal.input_schema, binding.pointer)
        if binding.field not in spec.state_fields:
            raise SchemaError("Unknown state field", code="UNKNOWN_STATE_FIELD")
        writers = report.writers[binding.field]
        if reader is not None and not writers.issubset(report.ancestors[reader]):
            raise SchemaError("Reader must wait for every writer", code="MISSING_DEPENDENCY")
        if not writers and isinstance(spec.state_fields[binding.field].initial, LiteralBinding):
            raise SchemaError(
                "Unproduced literal state is not a deliverable/input", code="UNPRODUCED_STATE"
            )
        return pointer_schema(spec.state_fields[binding.field].value_schema, binding.pointer)

    for index, node in enumerate(spec.nodes):
        path = f"/nodes/{index}"
        for key in ("input_schema", "output_schema"):
            guard(
                lambda: validate_schema(getattr(node, key), policy.max_schema_depth),
                path + "/" + key,
                node.id,
            )
        if node.kind != "llm":
            allowed = policy.allowed_tools if node.kind == "tool" else policy.allowed_evaluators
            entry = snapshot.entries.get(node.capability.key)
            if node.capability.key not in allowed:
                error(
                    "CAPABILITY_NOT_ALLOWED",
                    "Capability is not authorized",
                    path + "/capability",
                    node.id,
                )
            elif entry is None or entry.kind != node.kind:
                error(
                    "UNKNOWN_CAPABILITY",
                    "Capability or version does not exist",
                    path + "/capability",
                    node.id,
                )
            elif entry.read_only is not True:
                error(
                    "CAPABILITY_NOT_SUPPORTED",
                    "Only declared read-only capabilities are supported",
                    path,
                    node.id,
                )
            else:
                for key in ("input_schema", "output_schema"):

                    def check_contract(key=key):
                        left, right = getattr(node, key), getattr(entry, key)
                        if not equivalent(left, right):
                            raise SchemaError(
                                "Node schema differs from capability contract", code="TYPE_MISMATCH"
                            )

                    guard(check_contract, path + "/" + key, node.id)
        node_input = guard(lambda: expand(node.input_schema), path + "/input_schema", node.id)
        if node_input:
            if not set(node_input.get("required", [])).issubset(node.input_bindings):
                error("MISSING_INPUT_BINDING", "Required node input lacks a binding", path, node.id)
            for name, binding in node.input_bindings.items():

                def check_input(name=name, binding=binding):
                    target = node_input.get("properties", {}).get(
                        name, node_input["additionalProperties"]
                    )
                    if target is False:
                        raise SchemaError("Unexpected input binding", code="INVALID_BINDING")
                    if isinstance(binding, LiteralBinding):
                        validate_value(target, binding.literal)
                    elif not compatible(source_schema(binding, node.id), target):
                        raise SchemaError(
                            "Input assignment is not proven safe",
                            code="TYPE_COMPATIBILITY_UNPROVEN",
                        )

                guard(check_input, path + "/input_bindings/" + name, node.id)
        for write in node.writes:

            def check_write(write=write):
                if write.field not in spec.state_fields:
                    return
                if not compatible(
                    pointer_schema(node.output_schema, write.output_pointer),
                    spec.state_fields[write.field].update_schema,
                ):
                    raise SchemaError(
                        "Output is not compatible with update type", code="TYPE_MISMATCH"
                    )

            guard(check_write, path + "/writes", node.id)
        reads = {
            b.field
            for b in node.input_bindings.values()
            if not isinstance(b, LiteralBinding) and b.source == "state"
        }
        if reads & {w.field for w in node.writes}:
            error(
                "READ_WRITE_SAME_FIELD",
                "A node cannot read and write the same field",
                path,
                node.id,
            )
        if node.depends_on and not reads:
            error(
                "NO_DATA_DEPENDENCY",
                "Control-only dependencies are preserved",
                path,
                node.id,
                "warning",
            )
    target_output = expand(goal.output_schema)
    if not set(target_output.get("required", [])).issubset(spec.outputs):
        error("OUTPUT_CONTRACT_MISMATCH", "Required output mappings are missing", "/outputs")
    for key, binding in spec.outputs.items():

        def check_output(key=key, binding=binding):
            if binding.source != "state":
                raise SchemaError("Outputs require state references", code="INVALID_BINDING")
            target = target_output.get("properties", {}).get(
                key, target_output["additionalProperties"]
            )
            if target is False or not compatible(source_schema(binding), target):
                raise SchemaError(
                    "Final output assignment is incompatible", code="OUTPUT_CONTRACT_MISMATCH"
                )

        guard(check_output, "/outputs/" + key)
    if report.valid and len(canonical(report.initial_values)) > policy.max_state_bytes:
        error("PLAN_LIMIT_EXCEEDED", "Initial state exceeds byte limit")
    report.diagnostics.sort(key=lambda d: (d.path, d.code, d.node_id or ""))
    return report
