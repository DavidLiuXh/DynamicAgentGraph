"""Synthetic benchmark data, reference graphs and caller-owned acceptance checks."""

from dataclasses import dataclass

from ..api import DynamicGraphEngine
from ..capabilities.registry import EvaluatorDefinition, ReducerDefinition, ToolDefinition
from ..contracts import EngineConfig, ExecutionPolicy, GoalSpec
from ..graph.spec import GraphSpec
from ..models.client import FakeModelClient, ModelBindings


def obj(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def array(items):
    return {"type": "array", "items": items}


STRING, INTEGER = {"type": "string"}, {"type": "integer"}
EMPTY = obj({})


@dataclass
class Case:
    name: str
    category: str
    goal: GoalSpec
    graph: GraphSpec
    tools: list
    reducers: list
    expected: dict

    def engine(self, runs_dir, model=None, failure=False):
        fake = FakeModelClient([])
        engine = DynamicGraphEngine(
            config=EngineConfig(runs_dir=runs_dir),
            models=ModelBindings(model or fake, model or fake),
        )
        for definition in self.tools:
            if failure:
                from dataclasses import replace

                async def injected(data, context):
                    raise RuntimeError("Injected synthetic fixture failure")

                definition = replace(definition, handler=injected)
            if definition.kind == "check":
                engine.register_evaluator(definition)
            else:
                engine.register_tool(definition)
        for definition in self.reducers:
            engine.register_reducer(definition)
        return engine

    @property
    def policy(self):
        policy = ExecutionPolicy(
            allowed_tools=[f"{d.name}@{d.version}" for d in self.tools if d.kind == "tool"],
            allowed_evaluators=[f"{d.name}@{d.version}" for d in self.tools if d.kind == "check"],
        )
        policy.allowed_reducers.extend(f"{d.name}@{d.version}" for d in self.reducers)
        return policy

    def accepts(self, result):
        return result.execution_status == "COMPLETED" and result.outputs == self.expected


def fixture(
    name,
    category,
    objective,
    output_schema,
    initial,
    outputs,
    *,
    reducer="builtin.replace",
    reducer_config=None,
    update_schema=None,
    kind="tool",
    custom_reducers=(),
    output_key="result",
):
    tools, nodes = [], []
    update_schema = update_schema or output_schema
    # A supplied topic is a real bound input, never hardcoded as a tool credential.
    input_schema = obj({"topic": STRING})
    for index, output in enumerate(outputs):

        def bind_handler(output):
            async def handler(data, context):
                import copy

                return {"value": copy.deepcopy(output)}

            return handler

        capability_name = f"fixture.{name}_{index}"
        definition_type = EvaluatorDefinition if kind == "check" else ToolDefinition
        tools.append(
            definition_type(
                capability_name,
                "1.0.0",
                f"Read-only synthetic {name} data source {index}. Returns its authoritative value for the supplied topic.",
                input_schema,
                obj({"value": update_schema}),
                bind_handler(output),
            )
        )
        nodes.append(
            {
                "id": f"source_{index}",
                "kind": kind,
                "capability": {"name": capability_name, "version": "1.0.0"},
                "input_schema": input_schema,
                "input_bindings": {"topic": {"source": "input", "pointer": "/topic"}},
                "output_schema": obj({"value": update_schema}),
                "writes": [{"field": output_key, "output_pointer": "/value"}],
                "depends_on": [],
            }
        )
    graph = GraphSpec.model_validate(
        {
            "dsl_version": "1.0",
            "state_fields": {
                output_key: {
                    "value_schema": output_schema,
                    "update_schema": update_schema,
                    "initial": {"literal": initial},
                    "reducer": {
                        "name": reducer,
                        "version": "1.0.0",
                        "config": reducer_config or {},
                    },
                }
            },
            "nodes": nodes,
            "outputs": {output_key: {"source": "state", "field": output_key, "pointer": ""}},
        }
    )
    goal = GoalSpec(
        objective=objective,
        inputs={"topic": name},
        output_schema=obj({output_key: output_schema}),
        success_criteria=[
            {
                "id": "exact_sources",
                "description": "Use every supplied authorized source, preserve its data exactly, and return all requested fields.",
            }
        ],
    )
    if reducer == "builtin.merge_by_key":
        merged = {v[reducer_config["key"]]: v for batch in outputs for v in batch}
        expected = [merged[k] for k in sorted(merged)]
    elif reducer == "builtin.merge_map_strict":
        expected = {k: v for batch in outputs for k, v in batch.items()}
    elif custom_reducers:
        expected = {v["key"]: v["value"] for v in outputs}
    else:
        expected = outputs[0]
    return Case(name, category, goal, graph, tools, list(custom_reducers), {output_key: expected})


def add_processing(case, schema, initial, transform, output_key, description):
    """Extend a source fixture with a real data-dependent processing capability."""
    old_key = next(iter(case.expected))
    old_schema = case.graph.state_fields[old_key].value_schema.document()
    input_schema = obj({"data": old_schema})
    output_schema = obj({"value": schema})

    async def process(data, context):
        return {"value": transform(data["data"])}

    capability = f"fixture.{case.name}_process"
    case.tools.append(
        ToolDefinition(capability, "1.0.0", description, input_schema, output_schema, process)
    )
    graph = case.graph.document()
    graph["state_fields"][output_key] = {
        "value_schema": schema,
        "update_schema": schema,
        "initial": {"literal": initial},
        "reducer": {"name": "builtin.replace", "version": "1.0.0", "config": {}},
    }
    graph["nodes"].append(
        {
            "id": "process",
            "kind": "tool",
            "capability": {"name": capability, "version": "1.0.0"},
            "input_schema": input_schema,
            "input_bindings": {"data": {"source": "state", "field": old_key, "pointer": ""}},
            "output_schema": output_schema,
            "writes": [{"field": output_key, "output_pointer": "/value"}],
            "depends_on": [node.id for node in case.graph.nodes],
        }
    )
    graph["outputs"] = {output_key: {"source": "state", "field": output_key, "pointer": ""}}
    case.graph = GraphSpec.model_validate(graph)
    case.goal = GoalSpec(
        objective=case.goal.objective
        + " Use the registered processing capability on the source data.",
        inputs=case.goal.inputs,
        output_schema=obj({output_key: schema}),
        success_criteria=case.goal.success_criteria,
    )
    case.expected = {output_key: transform(case.expected[old_key])}
    return case


def cases():
    evidence = obj({"id": STRING, "text": STRING})
    result = [
        fixture(
            "literature",
            "research",
            "Collect findings from both synthetic literature sources; deduplicate by id, sort by id, and preserve text.",
            array(evidence),
            [],
            [[{"id": "a:1", "text": "Observation A"}], [{"id": "b:1", "text": "Observation B"}]],
            reducer="builtin.merge_by_key",
            reducer_config={"key": "id", "on_conflict": "error"},
            output_key="findings",
        ),
        fixture(
            "deduplication",
            "research",
            "Combine both citation sources. Duplicate IDs with identical content must appear once, sorted by id.",
            array(evidence),
            [],
            [
                [{"id": "same", "text": "Repeated citation"}],
                [{"id": "same", "text": "Repeated citation"}],
            ],
            reducer="builtin.merge_by_key",
            reducer_config={"key": "id", "on_conflict": "error"},
            output_key="citations",
        ),
        fixture(
            "catalog",
            "research",
            "Combine both source catalogs into one dictionary, preserving every entry.",
            {"type": "object", "additionalProperties": STRING},
            {},
            [{"a": "source:A"}, {"b": "source:B"}],
            reducer="builtin.merge_map_strict",
            output_key="catalog",
        ),
        fixture(
            "check_evidence",
            "research",
            "Run the supplied citation checker and return its exact evidence, even when valid is false.",
            obj({"valid": {"type": "boolean"}, "reason": STRING}),
            {"valid": False, "reason": ""},
            [{"valid": False, "reason": "Synthetic citation is unavailable"}],
            kind="check",
            output_key="check",
        ),
        fixture(
            "nullable_source",
            "research",
            "Read the source metadata. Preserve an unknown author as null rather than inventing an author.",
            obj({"title": STRING, "author": {"type": ["string", "null"]}}),
            {"title": "", "author": None},
            [{"title": "Synthetic note", "author": None}],
            output_key="metadata",
        ),
    ]
    value = {"type": "object", "additionalProperties": INTEGER}
    update = obj({"key": STRING, "value": INTEGER})

    def index(old, delta, config):
        if delta["key"] in old and old[delta["key"]] != delta["value"]:
            raise ValueError("Conflicting entry")
        return {**old, delta["key"]: delta["value"]}

    custom = ReducerDefinition(
        "fixture.index",
        "1.0.0",
        "Merge one key/value entry into an integer-valued dictionary; initial must be empty; parallel-safe for distinct keys, conflicts rejected.",
        value,
        update,
        EMPTY,
        lambda value: value == {},
        index,
        parallel_safe=True,
    )
    result.append(
        fixture(
            "evidence_counts",
            "research",
            "Combine both evidence-count entries into a dictionary indexed by key using the registered index reducer.",
            value,
            {},
            [{"key": "paper_a", "value": 2}, {"key": "paper_b", "value": 3}],
            reducer="fixture.index",
            update_schema=update,
            custom_reducers=[custom],
            output_key="counts",
        )
    )
    result.extend(
        [
            add_processing(
                fixture(
                    "sales_totals",
                    "data",
                    "Collect sales transactions from both sources and compute exact totals by region.",
                    array(obj({"id": STRING, "region": STRING, "amount": INTEGER})),
                    [],
                    [
                        [{"id": "1", "region": "east", "amount": 10}],
                        [
                            {"id": "2", "region": "east", "amount": 20},
                            {"id": "3", "region": "west", "amount": 50},
                        ],
                    ],
                    reducer="builtin.merge_by_key",
                    reducer_config={"key": "id", "on_conflict": "error"},
                    output_key="transactions",
                ),
                {"type": "object", "additionalProperties": INTEGER},
                {},
                lambda rows: {
                    region: sum(r["amount"] for r in rows if r["region"] == region)
                    for region in sorted({r["region"] for r in rows})
                },
                "totals",
                "Compute exact integer sales totals grouped by region from the full transaction array.",
            ),
            add_processing(
                fixture(
                    "normalized_names",
                    "data",
                    "Read the raw names, trim whitespace and uppercase them while preserving order.",
                    array(STRING),
                    [],
                    [[" Alice ", "bob"]],
                    output_key="raw_names",
                ),
                array(STRING),
                [],
                lambda names: [n.strip().upper() for n in names],
                "names",
                "Normalize each supplied name by stripping whitespace and uppercasing; preserve order.",
            ),
            fixture(
                "inventory",
                "data",
                "Combine inventory from the two warehouses; preserve SKU and count, sort by SKU.",
                array(obj({"sku": STRING, "count": INTEGER})),
                [],
                [[{"sku": "A", "count": 2}], [{"sku": "B", "count": 5}]],
                reducer="builtin.merge_by_key",
                reducer_config={"key": "sku", "on_conflict": "error"},
                output_key="inventory",
            ),
            add_processing(
                fixture(
                    "statistics",
                    "data",
                    "Read the integer observations and compute their count, minimum and maximum.",
                    array(INTEGER),
                    [],
                    [[-2, 3, 4, 8]],
                    output_key="observations",
                ),
                obj({"count": INTEGER, "minimum": INTEGER, "maximum": INTEGER}),
                {"count": 0, "minimum": 0, "maximum": 0},
                lambda values: {
                    "count": len(values),
                    "minimum": min(values),
                    "maximum": max(values),
                },
                "statistics",
                "Compute count, minimum and maximum of the supplied nonempty integer dataset.",
            ),
            fixture(
                "nested_records",
                "data",
                "Read the nested customer record and preserve the address and ordered tags.",
                obj({"name": STRING, "address": obj({"city": STRING}), "tags": array(STRING)}),
                {"name": "", "address": {"city": ""}, "tags": []},
                [{"name": "Ada", "address": {"city": "Shanghai"}, "tags": ["test", "active"]}],
                output_key="customer",
            ),
            fixture(
                "classification",
                "data",
                "Read the classification dataset and return its enum label and supporting integer score.",
                obj(
                    {
                        "label": {"type": "string", "enum": ["low", "high"]},
                        "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    }
                ),
                {"label": "low", "score": 0},
                [{"label": "high", "score": 85}],
                output_key="classification",
            ),
        ]
    )
    return result
