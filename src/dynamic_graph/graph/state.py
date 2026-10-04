from copy import deepcopy
from typing import Annotated, TypedDict

from ..execution.errors import RunFailure


def make_cell_reducer(bound, field_name):
    def merge(previous, incoming):
        if incoming["kind"] == "init":
            if previous:
                raise RunFailure("REDUCER_FAILED", "State initialized twice")
            return {"kind": "value", "value": bound.initial(incoming["value"])}
        if not previous or previous.get("kind") != "value":
            raise RunFailure("REDUCER_FAILED", "Update before state initialization")
        try:
            value = bound.apply(previous["value"], incoming["value"])
        except RunFailure as exc:
            raise RunFailure(
                exc.code, str(exc), details={**exc.details, "field": field_name}
            ) from exc
        return {"kind": "value", "value": value}

    return merge


def build_state(report):
    annotations = {
        name: Annotated[dict, make_cell_reducer(bound, name)]
        for name, bound in report.reducers.items()
    }
    annotations["__committed_nodes"] = Annotated[dict, lambda old, delta: {**old, **delta}]
    return TypedDict("DynamicState", annotations)


def initial_state(report):
    return {
        **{
            name: {"kind": "init", "value": deepcopy(value)}
            for name, value in report.initial_values.items()
        },
        "__committed_nodes": {},
    }


def unwrap(state):
    """Expose cell values for internal reads; binding_value copies selected inputs."""
    return {
        name: cell["value"]
        for name, cell in state.items()
        if not name.startswith("__") and cell.get("kind") == "value"
    }
