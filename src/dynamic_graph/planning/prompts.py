import hashlib
import json
import unicodedata
from importlib.resources import files

from ..capabilities.catalog import BUILTIN_REDUCER_CATALOG
from ..graph.schemas import canonical
from .responses import PlanningResponse


def examples():
    root = files("dynamic_graph.planning") / "examples"
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(root.iterdir(), key=lambda p: p.name)
        if path.name.endswith(".json")
    ]


def prompt_language(objective):
    """Use Chinese for objectives containing Han characters, English otherwise."""
    return (
        "zh"
        if any(
            unicodedata.name(character, "").startswith(
                ("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH")
            )
            for character in objective
        )
        else "en"
    )


def resources(language="zh"):
    if language not in {"zh", "en"}:
        raise ValueError("Planner prompt language must be zh or en")
    root = files("dynamic_graph.planning") / "prompts"
    suffix = "_en" if language == "en" else ""
    system = (root / f"planner_system_v1{suffix}.txt").read_text(encoding="utf-8")
    repair = (root / f"planner_repair_v1{suffix}.txt").read_text(encoding="utf-8")
    schema = PlanningResponse.model_json_schema()

    def digest(value):
        return hashlib.sha256(value).hexdigest()

    return (
        system,
        repair,
        schema,
        {
            "prompt_version": "1.0",
            "prompt_language": language,
            "prompt_hash": digest(system.encode()),
            "repair_hash": digest(repair.encode()),
            "schema_hash": digest(canonical(schema)),
            "dsl_version": "1.0",
            "examples_hash": digest(canonical(examples())),
        },
    )


def planning_data(goal, snapshot, policy, previous=None, errors=None, attempt=1):
    available = set(snapshot.entries) | set(policy.allowed_reducers)

    def applicable(example):
        required = set(example["capability_keys"])
        graph = example["response"].get("graph")
        if graph:
            for node in graph["nodes"]:
                ref = node.get("capability")
                if ref:
                    required.add(ref["name"] + "@" + ref["version"])
            for field in graph["state_fields"].values():
                ref = field["reducer"]
                required.add(ref["name"] + "@" + ref["version"])
        return required.issubset(available)

    data = {
        "goal": goal.model_dump(mode="json", by_alias=True, exclude_none=True),
        "allowed_capabilities": snapshot.catalog(),
        "builtin_reducers": [
            {
                name: BUILTIN_REDUCER_CATALOG[key][name]
                for name in ("name", "version", "kind", "rule")
            }
            for key in policy.allowed_reducers
            if key in BUILTIN_REDUCER_CATALOG
        ],
        "execution_constraints": policy.model_dump(mode="json"),
        "dsl_rules_version": "1.0",
        "examples": [example for example in examples() if applicable(example)],
    }
    if previous is not None or errors:
        data["repair"] = {
            "previous_response": previous,
            "validation_errors": errors or [],
            "attempt": attempt,
            "remaining_rounds": policy.max_planning_rounds - attempt,
        }
    return data
