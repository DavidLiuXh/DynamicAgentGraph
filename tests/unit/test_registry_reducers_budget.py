import asyncio
from dataclasses import replace

import pytest

from dynamic_graph import CancellationToken, ReducerDefinition, ToolDefinition
from dynamic_graph.capabilities.builtins import assert_reducer_permutations, bind_reducer
from dynamic_graph.contracts import ExecutionPolicy, RegistrationError
from dynamic_graph.execution.budget import Budget
from dynamic_graph.execution.errors import RunFailure
from dynamic_graph.graph.spec import StateField

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


async def test_snapshot_preserves_bound_service_and_copies_only_metadata(setup_run):
    import threading

    class Service:
        def __init__(self):
            self.lock = threading.Lock()  # External resources cannot be deep-copied.
            self.calls = 0

        async def handle(self, data, context):
            self.calls += 1
            return {}

    service = Service()
    engine, _, policy, _ = setup_run([])
    definition = ToolDefinition(
        "demo.service", "1.0.0", "Bound service", dict(EMPTY), dict(EMPTY), service.handle
    )
    engine.register_tool(definition)
    definition.input_schema["description"] = "changed after registration"
    policy.allowed_tools.append("demo.service@1.0.0")
    snapshot = engine._registry.snapshot(policy)
    saved = snapshot.entries["demo.service@1.0.0"]
    assert saved.handler.__self__ is service
    assert "description" not in saved.input_schema
    await saved.handler({}, None)
    assert service.calls == 1


def test_registration_isolated_snapshots_and_readonly_query(setup_run):
    engine, _, policy, _ = setup_run([])
    snapshot = engine._registry.snapshot(policy)
    info = engine.get_capability("demo.search_a", "1.0.0")
    assert "handler" not in info.to_dict()
    info.to_dict()["description"] = "changed"
    assert engine.get_capability("demo.search_a", "1.0.0").description == "Synthetic search"
    original = snapshot.entries["demo.search_a@1.0.0"].handler

    async def new_handler(data, context):
        return {}

    new_def = ToolDefinition("demo.new", "1.0.0", "new", EMPTY, EMPTY, new_handler)
    engine.register_tool(new_def)
    assert "demo.new@1.0.0" not in snapshot.entries
    assert snapshot.entries["demo.search_a@1.0.0"].handler is original
    with pytest.raises(RegistrationError):
        engine.register_tool(new_def)
    with pytest.raises(RegistrationError):
        engine.register_tool(replace(new_def, name="builtin.new"))
    with pytest.raises(RegistrationError):
        engine.register_tool(replace(new_def, handler=lambda a, b: {}))
    other, _, _, _ = setup_run([])
    assert other.get_capability("demo.new", "1.0.0") is None


def test_snapshot_catalog_and_other_runs_do_not_share_mutable_schemas(setup_run):
    engine, _, policy, _ = setup_run([])
    first = engine._registry.snapshot(policy)
    second = engine._registry.snapshot(policy)
    key = "demo.search_a@1.0.0"
    catalog = first.catalog()
    catalog[0]["input_schema"]["properties"]["query"]["type"] = "integer"
    first.entries[key].input_schema["properties"]["query"]["type"] = "boolean"
    assert second.entries[key].input_schema["properties"]["query"]["type"] == "string"
    fresh = engine._registry.snapshot(policy)
    assert fresh.entries[key].input_schema["properties"]["query"]["type"] == "string"


@pytest.mark.parametrize("mutated_argument", ["old", "update", "config"])
def test_reducer_mutation_is_rejected_without_touching_caller_data(mutated_argument):
    from dynamic_graph.capabilities.builtins import BoundReducer

    def mutate(old, update, config):
        arguments = {"old": old, "update": update, "config": config["tags"]}
        arguments[mutated_argument].append("unexpected")
        return update

    schema = {"type": "array", "items": {"type": "string"}}
    config = {"tags": []}
    reducer = BoundReducer(
        handler=mutate,
        parallel_safe=False,
        value_schema=schema,
        update_schema=schema,
        config=config,
        initial_validator=lambda value: True,
    )
    old, update = ["old"], ["new"]
    with pytest.raises(RunFailure) as failure:
        reducer.apply(old, update)
    assert failure.value.code == "REDUCER_FAILED"
    assert old == ["old"] and update == ["new"] and config == {"tags": []}


def test_reducer_result_isolated_from_references_retained_by_extension():
    from dynamic_graph.capabilities.builtins import BoundReducer

    retained = []

    def handler(old, update, config):
        retained.append(update)
        return update

    schema = {"type": "array", "items": {"type": "string"}}
    reducer = BoundReducer(
        handler=handler,
        parallel_safe=False,
        value_schema=schema,
        update_schema=schema,
        config={},
        initial_validator=lambda value: value == [],
    )
    initial = reducer.initial([])
    result = reducer.apply(initial, ["new"])
    for value in retained:
        value.append("late mutation")
    assert initial == [] and result == ["new"]


def test_initial_value_is_checked_and_isolated_from_caller():
    from dynamic_graph.capabilities.builtins import BoundReducer
    from dynamic_graph.graph.schemas import SchemaError

    schema = {"type": "array", "items": {"type": "string"}}
    reducer = BoundReducer(
        handler=lambda old, update, config: update,
        parallel_safe=False,
        value_schema=schema,
        update_schema=schema,
        config={},
        initial_validator=lambda value: len(value) == 1,
    )
    source = ["initial"]
    initial = reducer.initial(source)
    source.append("caller mutation")
    assert initial == ["initial"]
    initial.append("state mutation")
    assert source == ["initial", "caller mutation"]
    with pytest.raises(SchemaError, match="rejects initial"):
        reducer.initial([])


def test_distinct_value_update_and_permutation_contract(setup_run):
    engine, _, policy, _ = setup_run([])
    value = {"type": "object", "additionalProperties": {"type": "integer"}}
    update = {
        "type": "object",
        "properties": {"key": {"type": "string"}, "value": {"type": "integer"}},
        "required": ["key", "value"],
        "additionalProperties": False,
    }
    calls = []

    def handler(old, delta, config):
        calls.append(delta["key"])
        return {**old, delta["key"]: delta["value"]}

    definition = ReducerDefinition(
        "demo.index",
        "1.0.0",
        "Index an entry",
        value,
        update,
        EMPTY,
        lambda x: x == {},
        handler,
        parallel_safe=True,
    )
    engine.register_reducer(definition)
    policy.allowed_reducers.append("demo.index@1.0.0")
    field = StateField.model_validate(
        {
            "value_schema": value,
            "update_schema": update,
            "initial": {"literal": {}},
            "reducer": {"name": "demo.index", "version": "1.0.0", "config": {}},
        }
    )
    reducer = bind_reducer(field, engine._registry.snapshot(policy), policy)
    assert reducer.initial({}) == {} and not calls
    assert_reducer_permutations(reducer, {}, [{"key": "a", "value": 1}, {"key": "b", "value": 2}])
    assert len(calls) == 200


def test_merge_conflict_dedup_and_input_immutability(setup_run, reference):
    engine, _, policy, _ = setup_run([])
    reducer = bind_reducer(
        StateField.model_validate(reference["state_fields"]["findings"]),
        engine._registry.snapshot(policy),
        policy,
    )
    one = [{"id": "a", "text": "A"}]
    assert reducer.apply(one, one) == one
    with pytest.raises(RunFailure):
        reducer.apply(one, [{"id": "a", "text": "different"}])
    assert one == [{"id": "a", "text": "A"}]
    updates = [one, [{"id": "b", "text": "B"}], one]
    assert_reducer_permutations(reducer, [], updates)
    assert updates == [one, [{"id": "b", "text": "B"}], one]
    assert one == [{"id": "a", "text": "A"}]


async def test_budget_reservation_is_atomic():
    budget = Budget(ExecutionPolicy(max_model_calls=3), CancellationToken())
    results = await asyncio.gather(
        *(budget.reserve("model") for _ in range(20)), return_exceptions=True
    )
    assert sum(r is None for r in results) == 3
    assert budget.model_calls == 3
    assert sum(isinstance(r, RunFailure) for r in results) == 17
    assert budget.summary()["input_tokens"] is None
