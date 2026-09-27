import argparse
import importlib
import json
from pathlib import Path

from ..contracts import ExecutionPolicy, GoalSpec
from ..graph.compiler import compile_graph
from ..graph.schemas import strict_loads
from ..graph.spec import GraphSpec
from ..graph.validation import validate_graph
from .fixtures import cases


def main(compile=False):
    parser = argparse.ArgumentParser(
        description="Offline graph validation and deterministic compilation"
    )
    parser.add_argument("graph", type=Path)
    parser.add_argument("--goal", required=True, type=Path)
    parser.add_argument("--policy", type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--demo", choices=[c.name for c in cases()])
    group.add_argument(
        "--factory",
        help="Explicit trusted module:function returning a configured Engine; never read from GraphSpec",
    )
    args = parser.parse_args()
    if args.demo:
        case = next(c for c in cases() if c.name == args.demo)
        engine, policy = case.engine("runs"), case.policy
    else:
        module, function = args.factory.split(":", 1)
        engine = getattr(importlib.import_module(module), function)()
        policy = ExecutionPolicy()
    if args.policy:
        policy = ExecutionPolicy.model_validate(strict_loads(args.policy.read_text()))
    raw_graph = args.graph.read_bytes()
    if len(raw_graph) > policy.max_graph_bytes:
        parser.error("Graph exceeds byte limit")
    goal = GoalSpec.model_validate(strict_loads(args.goal.read_text()))
    spec = GraphSpec.model_validate(strict_loads(raw_graph))
    snapshot = engine._registry.snapshot(policy)
    report = validate_graph(spec, goal, snapshot, policy)
    result = {
        "valid": report.valid,
        "diagnostics": [d.model_dump(mode="json") for d in report.diagnostics],
    }
    if report.valid and compile:
        result["summary"] = compile_graph(spec, goal, snapshot, report).summary
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if report.valid else 1


def check_main():
    raise SystemExit(main())


def compile_main():
    raise SystemExit(main(compile=True))
