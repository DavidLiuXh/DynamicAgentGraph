"""An installed-package, network-free end-to-end demonstration."""

import asyncio
import json

from ..models.client import FakeModelClient
from .fixtures import cases


async def run():
    case = next(c for c in cases() if c.name == "sales_totals")
    planner = FakeModelClient(
        [
            {
                "response_version": "1.0",
                "outcome": "graph",
                "graph": case.graph.document(),
                "diagnostics": [],
            }
        ]
    )
    engine = case.engine("runs", planner)
    result = await engine.run(goal=case.goal, policy=case.policy)
    print(
        json.dumps(
            {
                "execution_status": result.execution_status,
                "outputs": result.outputs,
                "caller_accepted": case.accepts(result),
                "graph_ref": result.graph_ref,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if case.accepts(result) else 1


def main():
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
