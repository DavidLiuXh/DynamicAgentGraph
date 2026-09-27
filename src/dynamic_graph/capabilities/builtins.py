"""Specialize trusted reducers to per-graph value and update schemas."""

import copy
import random
from collections.abc import Callable
from dataclasses import dataclass

from ..contracts import BUILTIN_REDUCERS
from ..execution.errors import RunFailure
from ..graph.schemas import SchemaError, canonical, compatible_expanded, expand, validate_value


@dataclass(frozen=True)
class BoundReducer:
    handler: Callable
    parallel_safe: bool
    value_schema: dict
    update_schema: dict
    config: dict
    initial_validator: Callable

    def initial(self, value):
        validate_value(self.value_schema, value)
        validated = copy.deepcopy(value)
        # Trusted validators must only inspect their argument, including after return.
        result = self.initial_validator(validated)
        if result is False:
            raise SchemaError("Reducer rejects initial value")
        return validated

    def apply(self, old, update):
        validate_value(self.value_schema, old)
        validate_value(self.update_schema, update)
        # Extension handlers must never receive references to committed state.
        old, update = copy.deepcopy(old), copy.deepcopy(update)
        config = copy.deepcopy(self.config)
        before = canonical([old, update, config])
        try:
            result = self.handler(old, update, config)
            if before != canonical([old, update, config]):
                raise ValueError("Reducer mutated its arguments")
            validate_value(self.value_schema, result)
            return copy.deepcopy(result)
        except Exception as exc:
            raise RunFailure("REDUCER_FAILED", "Reducer update violated its contract") from exc


def bind_reducer(field, snapshot, policy):
    binding = field.reducer
    if binding.key not in policy.allowed_reducers:
        raise SchemaError("Reducer is not allowed", code="CAPABILITY_NOT_ALLOWED")
    value, update = expand(field.value_schema), expand(field.update_schema)
    config = copy.deepcopy(binding.config)
    if binding.key in BUILTIN_REDUCERS:
        if not (compatible_expanded(value, update) and compatible_expanded(update, value)):
            raise SchemaError("Built-in reducer requires equivalent V and U")
        if binding.name == "builtin.replace":
            if config:
                raise SchemaError("replace config must be empty")
            return BoundReducer(
                handler=lambda old, delta, cfg: delta,
                parallel_safe=False,
                value_schema=value,
                update_schema=update,
                config={},
                initial_validator=lambda _: True,
            )
        if binding.name == "builtin.merge_map_strict":
            if (
                config
                or value.get("type") != "object"
                or not isinstance(value.get("additionalProperties"), dict)
                or value.get("properties")
                or value.get("required")
            ):
                raise SchemaError("merge_map_strict requires a homogeneous typed dictionary")

            def merge_map(old, delta, _):
                merged = dict(old)
                for key, item in delta.items():
                    if key in merged and canonical(merged[key]) != canonical(item):
                        raise ValueError("Conflicting key")
                    merged[key] = item
                return dict(sorted(merged.items()))

            return BoundReducer(
                handler=merge_map,
                parallel_safe=True,
                value_schema=value,
                update_schema=update,
                config={},
                initial_validator=lambda initial: initial == {},
            )
        if set(config) != {"key", "on_conflict"} or config["on_conflict"] != "error":
            raise SchemaError("merge_by_key requires key and on_conflict=error")
        item = value.get("items", {})
        key = config["key"]
        if (
            value.get("type") != "array"
            or item.get("type") != "object"
            or (key not in item.get("required", []))
            or item.get("properties", {}).get(key, {}).get("type") != "string"
        ):
            raise SchemaError("merge_by_key requires a required string key in each object")

        def merge_array(old, delta, cfg):
            merged = {}
            for entry in old + delta:
                ident = entry[cfg["key"]]
                if ident in merged and canonical(merged[ident]) != canonical(entry):
                    raise ValueError("Conflicting key")
                merged[ident] = entry
            return [merged[k] for k in sorted(merged)]

        return BoundReducer(
            handler=merge_array,
            parallel_safe=True,
            value_schema=value,
            update_schema=update,
            config=config,
            initial_validator=lambda initial: initial == [],
        )
    definition = snapshot.entries.get(binding.key)
    if definition is None or definition.kind != "reducer":
        raise SchemaError("Unknown reducer", code="UNKNOWN_CAPABILITY")
    for field_schema, registered_schema in [
        (value, definition.value_schema),
        (update, definition.update_schema),
    ]:
        registered_schema = expand(registered_schema)
        if not (
            compatible_expanded(field_schema, registered_schema)
            and compatible_expanded(registered_schema, field_schema)
        ):
            raise SchemaError("Custom reducer V/U contract differs from field schema")
    validate_value(definition.config_schema, config)
    return BoundReducer(
        handler=definition.handler,
        parallel_safe=definition.parallel_safe,
        value_schema=value,
        update_schema=update,
        config=config,
        initial_validator=definition.initial_validator,
    )


def assert_reducer_permutations(reducer, initial, updates, trials=100):
    """A deterministic test helper, not a proof of arbitrary extension correctness."""
    rng = random.Random(0)
    expected = None
    for _ in range(trials):
        ordered = list(updates)
        rng.shuffle(ordered)
        value = reducer.initial(initial)
        for delta in ordered:
            value = reducer.apply(value, delta)
        actual = canonical(value)
        if expected is None:
            expected = actual
        assert actual == expected, "Reducer result depends on update ordering"
